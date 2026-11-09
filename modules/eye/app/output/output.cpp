/**
 * Licensed under the MIT license.
 *
 * Local inference outputs (percept-kit addition): stdout, an HTTP stream (cpp-httplib) with Prometheus
 * metrics, and an MQTT publisher (Eclipse Paho MQTT C). Configuration is described in output.hpp. The
 * network outputs run on their own threads, so a slow client or an unreachable broker never stalls inference.
 */

// Standard library includes
#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

// System includes
#include <dirent.h>
#include <signal.h>
#include <unistd.h>

// Third party includes
#include <MQTTClient.h>
#include <VPUTelemetry.h>
#include <VPUTempRead.hpp>
#include <httplib.h>

// Local includes
#include "output.hpp"
#include "../util/helper.hpp"

namespace output {
namespace {

std::string env_str(const char *name, const std::string &def = "")
{
    const char *v = std::getenv(name);
    return (v != nullptr && *v != '\0') ? std::string(v) : def;
}

bool env_flag(const char *name)
{
    std::string v = env_str(name);
    std::transform(v.begin(), v.end(), v.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return v == "1" || v == "true" || v == "yes" || v == "on";
}

long env_long(const char *name, long def, long lo, long hi)
{
    std::string v = env_str(name);
    if (v.empty())
    {
        return def;
    }
    char *end = nullptr;
    long n = std::strtol(v.c_str(), &end, 10);
    if (*end != '\0' || n < lo || n > hi)
    {
        util::log_error(std::string(name) + "=" + v + " is not a number from " + std::to_string(lo) + " to " +
                        std::to_string(hi) + "; using " + std::to_string(def));
        return def;
    }
    return n;
}

// ------------------------------------------------------------------------------------------------
// stdout

std::mutex stdout_mutex;

void stdout_publish(const std::string &json)
{
    std::lock_guard<std::mutex> lock(stdout_mutex);
    std::cout << json + "\n" << std::flush;
}

// ------------------------------------------------------------------------------------------------
// Prometheus metrics, served as GET /metrics by the HTTP output. Every inference message is counted.
// The Myriad X's temperatures and memory are read from the inference thread, so only while the pipeline
// runs: mxIf::Boot() initialises both readers when the pipeline starts, and mxIf::Reset() tears them down.

double unix_seconds()
{
    return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
}

const double process_start_seconds = unix_seconds();

std::string read_line(const std::string &path)
{
    std::ifstream f(path);
    std::string line;
    std::getline(f, line);
    return line;
}

/** Find the Myriad X (USB vendor 03e7) in sysfs: product f63b once its firmware is booted, 2485 before. */
bool find_myriadx(std::string &product, long &speed_mbps)
{
    static const std::string base = "/sys/bus/usb/devices/";
    DIR *dir = opendir(base.c_str());
    if (dir == nullptr)
    {
        return false;
    }
    bool found = false;
    while (struct dirent *entry = readdir(dir))
    {
        std::string dev = base + entry->d_name + "/";
        if (entry->d_name[0] == '.' || read_line(dev + "idVendor") != "03e7")
        {
            continue;
        }
        product = read_line(dev + "idProduct");
        speed_mbps = std::strtol(read_line(dev + "speed").c_str(), nullptr, 10);
        found = true;
        break;
    }
    closedir(dir);
    return found;
}

std::string num(double value, int decimals)
{
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f", decimals, value);
    return buf;
}

std::string escape_label(const std::string &value)
{
    std::string out;
    for (char c : value)
    {
        if (c == '\\' || c == '"')
        {
            out += '\\';
            out += c;
        }
        else if (c == '\n')
        {
            out += "\\n";
        }
        else
        {
            out += c;
        }
    }
    return out;
}

// Gauges older than this (or three telemetry intervals) are left out of /metrics: the inference thread updates
// them, so a stalled pipeline would otherwise keep reporting its last values as current.
const double kStaleSeconds = 15.0;

class Metrics
{
public:
    void set_vpu_interval(long ms)
    {
        vpu_interval_ms_ = ms;
    }

    /** Count one inference message, and the objects in it by label. */
    void record(const std::string &json)
    {
        double now = unix_seconds();
        std::map<std::string, uint64_t> seen;
        static const std::string label_key = "\"label\": \"";
        for (size_t pos = json.find(label_key); pos != std::string::npos; pos = json.find(label_key, pos))
        {
            pos += label_key.size();
            size_t end = json.find('"', pos);
            if (end == std::string::npos)
            {
                break;
            }
            ++seen[json.substr(pos, end - pos)];
            pos = end;
        }

        std::lock_guard<std::mutex> lock(mutex_);
        ++inferences_;
        last_inference_ = now;
        for (auto &kv : objects_)
        {
            kv.second = 0;
        }
        for (const auto &kv : seen)
        {
            std::string key = series_label(kv.first);
            objects_[key] += kv.second;
            detections_[key] += kv.second;
        }
    }

    /** Read the Myriad X's temperatures and memory, at most once per interval. Call only from the inference thread. */
    void sample_vpu()
    {
        if (vpu_interval_ms_ <= 0)
        {
            return;
        }
        auto now = std::chrono::steady_clock::now();
        if (vpu_sampled_ && now - last_vpu_sample_ < std::chrono::milliseconds(vpu_interval_ms_))
        {
            return;
        }
        vpu_sampled_ = true;
        last_vpu_sample_ = now;

        // Only a booted Myriad X has the readers; the device can't be booted by anyone but this process.
        std::string product;
        long speed = 0;
        if (!find_myriadx(product, speed) || product != "f63b")
        {
            return;
        }
        float temps[4] = {0.0f, 0.0f, 0.0f, 0.0f};
        bool temps_ok = vpual::tempread::stub::Get(&temps[0], &temps[1], &temps[2], &temps[3]) == vpual::tempread::GET_SUCCESS;
        vpual::telemetry::Memory::Info memory{};
        bool memory_ok = vpual::telemetry::Memory::Get(&memory) == 0;
        if (temps_ok != temps_ok_ || memory_ok != memory_ok_)
        {
            util::log_info(std::string("Myriad X telemetry: temperatures ") + (temps_ok ? "ok" : "unavailable") +
                           ", memory " + (memory_ok ? "ok" : "unavailable"));
        }

        std::lock_guard<std::mutex> lock(mutex_);
        temps_ok_ = temps_ok;
        memory_ok_ = memory_ok;
        if (temps_ok)
        {
            std::copy(std::begin(temps), std::end(temps), std::begin(temps_));
        }
        if (memory_ok)
        {
            memory_ = memory;
        }
        if (temps_ok || memory_ok)
        {
            vpu_sampled_at_ = unix_seconds();
        }
    }

    /** Prometheus text exposition format 0.0.4. Values a stalled pipeline can no longer refresh are left out. */
    std::string render()
    {
        std::string product;
        long speed = 0;
        bool usb = find_myriadx(product, speed);
        double now = unix_seconds();

        std::ostringstream o;
        std::lock_guard<std::mutex> lock(mutex_);
        bool inference_fresh = inferences_ > 0 && now - last_inference_ <= kStaleSeconds;
        bool vpu_fresh = vpu_sampled_at_ > 0.0 &&
                         now - vpu_sampled_at_ <= std::max(kStaleSeconds, 3.0 * static_cast<double>(vpu_interval_ms_) / 1000.0);
        o << "# HELP percept_eye_inferences_total Inference results produced by the Eye's model.\n"
             "# TYPE percept_eye_inferences_total counter\n"
             "percept_eye_inferences_total " << inferences_ << "\n";
        o << "# HELP percept_eye_detections_total Objects detected, by label.\n"
             "# TYPE percept_eye_detections_total counter\n";
        for (const auto &kv : detections_)
        {
            o << "percept_eye_detections_total{label=\"" << escape_label(kv.first) << "\"} " << kv.second << "\n";
        }
        o << "# HELP percept_eye_objects Objects in the latest inference, by label.\n"
             "# TYPE percept_eye_objects gauge\n";
        if (inference_fresh)
        {
            for (const auto &kv : objects_)
            {
                o << "percept_eye_objects{label=\"" << escape_label(kv.first) << "\"} " << kv.second << "\n";
            }
        }
        if (inferences_ > 0)
        {
            o << "# HELP percept_eye_last_inference_timestamp_seconds Unix time of the latest inference.\n"
                 "# TYPE percept_eye_last_inference_timestamp_seconds gauge\n"
                 "percept_eye_last_inference_timestamp_seconds " << num(last_inference_, 3) << "\n";
        }

        if (temps_ok_ && vpu_fresh)
        {
            static const char *sensors[4] = {"css", "mss", "upa", "dss"};
            o << "# HELP percept_myriadx_temperature_celsius Myriad X die temperature: css CPU subsystem, mss media subsystem, upa SHAVE array, dss DDR subsystem.\n"
                 "# TYPE percept_myriadx_temperature_celsius gauge\n";
            for (int i = 0; i < 4; ++i)
            {
                o << "percept_myriadx_temperature_celsius{sensor=\"" << sensors[i] << "\"} " << num(temps_[i], 3) << "\n";
            }
        }
        if (memory_ok_ && vpu_fresh)
        {
            const std::pair<const char *, const vpual::telemetry::Memory::Entry *> pools[3] = {
                {"main", &memory_.poolMain}, {"leonos_heap", &memory_.losHeap}, {"leonrt_heap", &memory_.lrtHeap}};
            o << "# HELP percept_myriadx_memory_used_bytes Myriad X memory in use, by pool.\n"
                 "# TYPE percept_myriadx_memory_used_bytes gauge\n";
            for (const auto &p : pools)
            {
                o << "percept_myriadx_memory_used_bytes{pool=\"" << p.first << "\"} " << p.second->used << "\n";
            }
            o << "# HELP percept_myriadx_memory_size_bytes Myriad X memory size, by pool.\n"
                 "# TYPE percept_myriadx_memory_size_bytes gauge\n";
            for (const auto &p : pools)
            {
                o << "percept_myriadx_memory_size_bytes{pool=\"" << p.first << "\"} " << p.second->total << "\n";
            }
        }
        if (vpu_sampled_at_ > 0.0)
        {
            o << "# HELP percept_myriadx_telemetry_timestamp_seconds Unix time the Myriad X temperatures and memory were read.\n"
                 "# TYPE percept_myriadx_telemetry_timestamp_seconds gauge\n"
                 "percept_myriadx_telemetry_timestamp_seconds " << num(vpu_sampled_at_, 3) << "\n";
        }
        if (usb)
        {
            o << "# HELP percept_myriadx_usb_speed_bits_per_second USB link speed of the Myriad X.\n"
                 "# TYPE percept_myriadx_usb_speed_bits_per_second gauge\n"
                 "percept_myriadx_usb_speed_bits_per_second " << speed * 1000000LL << "\n";
        }
        o << "# HELP process_start_time_seconds Start time of the process since unix epoch in seconds.\n"
             "# TYPE process_start_time_seconds gauge\n"
             "process_start_time_seconds " << num(process_start_seconds, 3) << "\n";
        return o.str();
    }

private:
    /** Caller holds mutex_. A model with many classes can't create unbounded series: beyond kMaxLabels, "other". */
    std::string series_label(const std::string &label) const
    {
        if (detections_.count(label) != 0 || detections_.size() < kMaxLabels)
        {
            return label;
        }
        return "other";
    }

    static const size_t kMaxLabels = 200;

    std::mutex mutex_;
    uint64_t inferences_ = 0;
    double last_inference_ = 0.0;
    std::map<std::string, uint64_t> detections_;
    std::map<std::string, uint64_t> objects_;

    // Written by the inference thread only, except where noted.
    long vpu_interval_ms_ = 5000;         // set before the HTTP server starts
    bool vpu_sampled_ = false;
    std::chrono::steady_clock::time_point last_vpu_sample_;
    bool temps_ok_ = false;               // under mutex_
    bool memory_ok_ = false;              // under mutex_
    float temps_[4] = {0.0f, 0.0f, 0.0f, 0.0f};  // under mutex_
    vpual::telemetry::Memory::Info memory_{};    // under mutex_
    double vpu_sampled_at_ = 0.0;         // under mutex_
};

Metrics metrics;

// ------------------------------------------------------------------------------------------------
// HTTP: GET /inferences streams NDJSON, GET /events streams Server-Sent Events, GET /latest returns
// the most recent message.

/** The last few messages, numbered, so that every streaming client can follow at its own pace. */
class Hub
{
public:
    void publish(const std::string &msg)
    {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ring_.push_back(msg);
            if (ring_.size() > kCapacity)
            {
                ring_.pop_front();
                ++first_;
            }
            ++next_;
        }
        cv_.notify_all();
    }

    bool latest(std::string &msg)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (ring_.empty())
        {
            return false;
        }
        msg = ring_.back();
        return true;
    }

    uint64_t next()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return next_;
    }

    /** Wait up to timeout_ms for messages numbered >= seq. A client that fell behind skips ahead. */
    bool wait(uint64_t &seq, std::vector<std::string> &out, int timeout_ms)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [&] { return next_ > seq; }))
        {
            return false;
        }
        seq = std::max(seq, first_);
        for (; seq < next_; ++seq)
        {
            out.push_back(ring_[static_cast<size_t>(seq - first_)]);
        }
        return true;
    }

private:
    static const size_t kCapacity = 64;
    std::deque<std::string> ring_;
    uint64_t first_ = 0;
    uint64_t next_ = 0;
    std::mutex mutex_;
    std::condition_variable cv_;
};

class HttpOutput
{
public:
    bool start(const std::string &bind, int port)
    {
        server_.set_read_timeout(5);
        server_.set_write_timeout(5);

        server_.Get("/", [](const httplib::Request &, httplib::Response &res) {
            res.set_content("Azure Percept Eye inference output\n"
                            "  GET /latest      most recent inference message (204 if none yet)\n"
                            "  GET /inferences  stream: one JSON message per line (NDJSON)\n"
                            "  GET /events      stream: Server-Sent Events\n"
                            "  GET /metrics     Prometheus metrics: inferences, detections, Myriad X temperature and memory\n",
                            "text/plain");
        });
        server_.Get("/latest", [this](const httplib::Request &, httplib::Response &res) {
            std::string msg;
            if (hub_.latest(msg))
            {
                res.set_header("Cache-Control", "no-store");
                res.set_content(msg + "\n", "application/json");
            }
            else
            {
                res.status = 204;
            }
        });
        server_.Get("/inferences", [this](const httplib::Request &, httplib::Response &res) { stream(res, false); });
        server_.Get("/events", [this](const httplib::Request &, httplib::Response &res) { stream(res, true); });
        server_.Get("/metrics", [](const httplib::Request &, httplib::Response &res) {
            res.set_content(metrics.render(), "text/plain; version=0.0.4; charset=utf-8");
        });

        if (!server_.bind_to_port(bind, port))
        {
            util::log_error("HTTP output disabled: cannot listen on " + bind + ":" + std::to_string(port));
            return false;
        }
        std::thread([this] { server_.listen_after_bind(); }).detach();
        util::log_info("HTTP output: listening on " + bind + ":" + std::to_string(port) +
                       " (/latest, /inferences, /events, /metrics)");
        return true;
    }

    void publish(const std::string &json)
    {
        hub_.publish(json);
    }

private:
    static const int kMaxStreams = 16;
    static const int kSseKeepAliveSeconds = 15;

    void stream(httplib::Response &res, bool sse)
    {
        if (streams_.fetch_add(1) >= kMaxStreams)
        {
            --streams_;
            res.status = 503;
            res.set_content("Too many streaming clients\n", "text/plain");
            return;
        }

        // Each wait is short so a disconnected client is noticed (cpp-httplib checks the socket between calls).
        auto seq = std::make_shared<uint64_t>(hub_.next());
        auto idle = std::make_shared<int>(0);
        res.set_header("Cache-Control", "no-store");
        res.set_chunked_content_provider(
            sse ? "text/event-stream" : "application/x-ndjson",
            [this, seq, idle, sse](size_t, httplib::DataSink &sink) -> bool {
                std::vector<std::string> msgs;
                if (!hub_.wait(*seq, msgs, 1000))
                {
                    if (sse && ++*idle >= kSseKeepAliveSeconds)
                    {
                        *idle = 0;
                        static const std::string ping = ": keep-alive\n\n";
                        return sink.write(ping.data(), ping.size());
                    }
                    return true;
                }
                *idle = 0;
                std::string chunk;
                for (const auto &m : msgs)
                {
                    chunk += sse ? "data: " + m + "\n\n" : m + "\n";
                }
                return sink.write(chunk.data(), chunk.size());
            },
            [this](bool) { --streams_; });
    }

    httplib::Server server_;
    Hub hub_;
    std::atomic<int> streams_{0};
};

// ------------------------------------------------------------------------------------------------
// MQTT: Paho's synchronous client, driven from one thread fed by a bounded queue.

/** Bounded FIFO between the inference thread and the MQTT thread. Drops the oldest message when full. */
class Queue
{
public:
    explicit Queue(size_t capacity) : capacity_(capacity) {}

    void push(const std::string &msg)
    {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (items_.size() >= capacity_)
            {
                items_.pop_front();
            }
            items_.push_back(msg);
        }
        cv_.notify_one();
    }

    /** Wait up to timeout_ms for a message. Returns false on timeout. */
    bool pop(std::string &msg, int timeout_ms)
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), [this] { return !items_.empty(); }))
        {
            return false;
        }
        msg = std::move(items_.front());
        items_.pop_front();
        return true;
    }

private:
    size_t capacity_;
    std::deque<std::string> items_;
    std::mutex mutex_;
    std::condition_variable cv_;
};

struct MqttConfig
{
    std::string url;
    std::string topic;
    std::string client_id;
    std::string username;
    std::string password;
    std::string ca_file;
    std::string cert_file;
    std::string key_file;
    int keepalive = 60;
    bool retain = false;
    bool tls = false;
    bool insecure = false;
};

bool has_prefix(const std::string &s, const char *prefix)
{
    return s.compare(0, std::char_traits<char>::length(prefix), prefix) == 0;
}

std::string connack_text(int rc)
{
    switch (rc)
    {
        case 1: return "refused: unacceptable protocol version";
        case 2: return "refused: client identifier rejected";
        case 3: return "refused: server unavailable";
        case 4: return "refused: bad user name or password";
        case 5: return "refused: not authorized";
        default: return MQTTClient_strerror(rc);
    }
}

int on_tls_error(const char *str, size_t len, void *)
{
    std::string text(str, len);
    while (!text.empty() && (text.back() == '\n' || text.back() == '\r'))
    {
        text.pop_back();
    }
    util::log_error("MQTT TLS: " + text);
    return 0;
}

class MqttOutput
{
public:
    explicit MqttOutput(const MqttConfig &cfg) : cfg_(cfg), queue_(256) {}

    bool start()
    {
        int rc = MQTTClient_create(&client_, cfg_.url.c_str(), cfg_.client_id.c_str(), MQTTCLIENT_PERSISTENCE_NONE, nullptr);
        if (rc != MQTTCLIENT_SUCCESS)
        {
            util::log_error("MQTT output disabled: " + std::string(MQTTClient_strerror(rc)) + " for EYE_MQTT_URL=" + cfg_.url +
                            " (use mqtt://, mqtts://, ws:// or wss://)");
            return false;
        }
        std::thread(&MqttOutput::run, this).detach();
        return true;
    }

    void publish(const std::string &json)
    {
        queue_.push(json);
    }

private:
    bool connect()
    {
        MQTTClient_connectOptions opts = MQTTClient_connectOptions_initializer;
        MQTTClient_SSLOptions ssl = MQTTClient_SSLOptions_initializer;
        opts.keepAliveInterval = cfg_.keepalive;
        opts.cleansession = 1;
        opts.connectTimeout = 10;
        opts.MQTTVersion = MQTTVERSION_3_1_1;
        if (!cfg_.username.empty())
        {
            opts.username = cfg_.username.c_str();
        }
        if (!cfg_.password.empty())
        {
            opts.password = cfg_.password.c_str();
        }
        if (cfg_.tls)
        {
            ssl.trustStore = cfg_.ca_file.empty() ? nullptr : cfg_.ca_file.c_str();
            ssl.keyStore = cfg_.cert_file.empty() ? nullptr : cfg_.cert_file.c_str();
            ssl.privateKey = cfg_.key_file.empty() ? nullptr : cfg_.key_file.c_str();
            ssl.enableServerCertAuth = cfg_.insecure ? 0 : 1;
            ssl.verify = cfg_.insecure ? 0 : 1;
            ssl.sslVersion = MQTT_SSL_VERSION_TLS_1_2;
            ssl.ssl_error_cb = on_tls_error;
            opts.ssl = &ssl;
        }

        int rc = MQTTClient_connect(client_, &opts);
        if (rc != MQTTCLIENT_SUCCESS)
        {
            util::log_error("MQTT: cannot connect to " + cfg_.url + ": " + connack_text(rc));
            return false;
        }
        util::log_info("MQTT: connected to " + cfg_.url);
        return true;
    }

    void run()
    {
        int backoff = 1;
        bool connected = false;
        auto last_yield = std::chrono::steady_clock::now();
        while (true)
        {
            if (!connected)
            {
                connected = connect();
                if (!connected)
                {
                    util::log_info("MQTT: retrying in " + std::to_string(backoff) + " s");
                    std::this_thread::sleep_for(std::chrono::seconds(backoff));
                    backoff = std::min(backoff * 2, 60);
                    continue;
                }
                backoff = 1;
            }

            std::string msg;
            if (queue_.pop(msg, 1000))
            {
                int rc = MQTTClient_publish(client_, cfg_.topic.c_str(), static_cast<int>(msg.size()), msg.data(), 0,
                                            cfg_.retain ? 1 : 0, nullptr);
                if (rc != MQTTCLIENT_SUCCESS)
                {
                    util::log_error("MQTT: publish failed: " + std::string(MQTTClient_strerror(rc)));
                    MQTTClient_disconnect(client_, 0);
                    connected = false;
                    continue;
                }
            }

            // Lets Paho send keep-alive pings and read the broker's replies.
            auto now = std::chrono::steady_clock::now();
            if (now - last_yield >= std::chrono::seconds(1))
            {
                last_yield = now;
                MQTTClient_yield();
            }
            if (!MQTTClient_isConnected(client_))
            {
                util::log_error("MQTT: connection to " + cfg_.url + " lost");
                connected = false;
            }
        }
    }

    MqttConfig cfg_;
    Queue queue_;
    MQTTClient client_ = nullptr;
};

// ------------------------------------------------------------------------------------------------

long interval_ms = 0;
bool use_stdout = false;
// The outputs live for the life of the process and their threads are detached, so they are never deleted.
HttpOutput *http = nullptr;
MqttOutput *mqtt = nullptr;
std::mutex publish_mutex;
bool published_once = false;
std::chrono::steady_clock::time_point last_publish;

void start_mqtt(const std::string &url)
{
    MqttConfig cfg;
    cfg.url = url;
    cfg.tls = has_prefix(url, "mqtts://") || has_prefix(url, "ssl://") || has_prefix(url, "wss://");

    char hostname[64] = {0};
    gethostname(hostname, sizeof(hostname) - 1);
    cfg.topic = env_str("EYE_MQTT_TOPIC", "percept/eye/inferences");
    cfg.client_id = env_str("EYE_MQTT_CLIENT_ID", ("eye-" + std::string(hostname)).substr(0, 23));
    cfg.username = env_str("EYE_MQTT_USERNAME");
    cfg.password = env_str("EYE_MQTT_PASSWORD");
    std::string password_file = env_str("EYE_MQTT_PASSWORD_FILE");
    if (!password_file.empty())
    {
        std::ifstream f(password_file);
        if (!f || !std::getline(f, cfg.password))
        {
            util::log_error("MQTT output disabled: cannot read EYE_MQTT_PASSWORD_FILE " + password_file);
            return;
        }
        if (!cfg.password.empty() && cfg.password.back() == '\r')
        {
            cfg.password.pop_back();
        }
    }
    cfg.keepalive = static_cast<int>(env_long("EYE_MQTT_KEEPALIVE", 60, 0, 65535));
    cfg.retain = env_flag("EYE_MQTT_RETAIN");
    cfg.ca_file = env_str("EYE_MQTT_CA_FILE");
    cfg.cert_file = env_str("EYE_MQTT_CERT_FILE");
    cfg.key_file = env_str("EYE_MQTT_KEY_FILE");
    cfg.insecure = env_flag("EYE_MQTT_INSECURE");

    if (cfg.topic.find_first_of("+#") != std::string::npos)
    {
        util::log_error("MQTT output disabled: EYE_MQTT_TOPIC must not contain + or #");
        return;
    }
    if (!cfg.tls && (!cfg.ca_file.empty() || !cfg.cert_file.empty() || cfg.insecure))
    {
        util::log_error("MQTT output: TLS settings are ignored for " + url + " (use mqtts:// or wss://)");
    }
    if (cfg.tls && cfg.insecure)
    {
        util::log_error("MQTT output: EYE_MQTT_INSECURE is set; the broker's certificate is NOT verified");
    }

    std::unique_ptr<MqttOutput> out(new MqttOutput(cfg));
    if (out->start())
    {
        mqtt = out.release();
        util::log_info("MQTT output: publishing to " + url + " topic '" + cfg.topic + "' as '" + cfg.client_id + "'");
    }
}

} // namespace

void start()
{
    // A client or broker that disconnects must not kill the app.
    signal(SIGPIPE, SIG_IGN);

    interval_ms = env_long("EYE_OUTPUT_INTERVAL_MS", 0, 0, 86400000);
    use_stdout = env_flag("EYE_STDOUT");
    if (use_stdout)
    {
        util::log_info("stdout output: one JSON line per inference");
    }

    long port = env_long("EYE_HTTP_PORT", 0, 0, 65535);
    if (port > 0)
    {
        // Before the server starts, so /metrics only ever reads it.
        long vpu_ms = env_long("EYE_VPU_TELEMETRY_MS", 5000, 0, 3600000);
        metrics.set_vpu_interval(vpu_ms);
        std::unique_ptr<HttpOutput> server(new HttpOutput());
        if (server->start(env_str("EYE_HTTP_BIND", "0.0.0.0"), static_cast<int>(port)))
        {
            http = server.release();
            if (vpu_ms == 0)
            {
                util::log_info("Myriad X telemetry off (EYE_VPU_TELEMETRY_MS=0)");
            }
        }
    }

    std::string url = env_str("EYE_MQTT_URL");
    if (!url.empty())
    {
        start_mqtt(url);
    }

    if (interval_ms > 0 && (use_stdout || http || mqtt))
    {
        util::log_info("Inference outputs send at most one message every " + std::to_string(interval_ms) + " ms");
    }
}

void publish(const std::string &json)
{
    // Metrics count every inference, before EYE_OUTPUT_INTERVAL_MS thins out the messages.
    if (http)
    {
        metrics.record(json);
        metrics.sample_vpu();
    }
    if (!use_stdout && !http && !mqtt)
    {
        return;
    }
    if (interval_ms > 0)
    {
        std::lock_guard<std::mutex> lock(publish_mutex);
        auto now = std::chrono::steady_clock::now();
        if (published_once && now - last_publish < std::chrono::milliseconds(interval_ms))
        {
            return;
        }
        published_once = true;
        last_publish = now;
    }

    if (use_stdout)
    {
        stdout_publish(json);
    }
    if (http)
    {
        http->publish(json);
    }
    if (mqtt)
    {
        mqtt->publish(json);
    }
}

} // namespace output
