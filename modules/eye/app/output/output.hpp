/**
 * Licensed under the MIT license.
 *
 * Local inference outputs (percept-kit addition). Every inference message the app would send to
 * IoT Edge ({"NEURAL_NETWORK": [...]}) is also handed to these outputs, each enabled and configured
 * by environment variables:
 *
 *   EYE_OUTPUT_INTERVAL_MS   minimum ms between messages for all outputs below (default 0 = every inference)
 *   EYE_STDOUT=1             print each message as one JSON line on stdout (docker logs)
 *   EYE_HTTP_PORT=<port>     serve GET /latest, /inferences (NDJSON stream), /events (SSE) - cpp-httplib -
 *                            and GET /metrics (Prometheus: inference counts, Myriad X temperature and memory)
 *   EYE_HTTP_BIND            listen address (default 0.0.0.0)
 *   EYE_VPU_TELEMETRY_MS     how often /metrics reads the Myriad X's temperatures and memory (default 5000, 0 = off)
 *   EYE_MQTT_URL             publish each message (MQTT 3.1.1, QoS 0) - Eclipse Paho MQTT C:
 *                            mqtt://host[:1883], mqtts://host[:8883], ws://host[:80]/path, wss://host[:443]/path
 *   EYE_MQTT_TOPIC           default percept/eye/inferences
 *   EYE_MQTT_CLIENT_ID       default eye-<hostname> (23 characters at most)
 *   EYE_MQTT_USERNAME, EYE_MQTT_PASSWORD or EYE_MQTT_PASSWORD_FILE
 *   EYE_MQTT_KEEPALIVE       seconds (default 60)
 *   EYE_MQTT_RETAIN=1        publish with the retain flag
 *   EYE_MQTT_CA_FILE         CA bundle (PEM) for mqtts:// / wss:// (default: the system bundle)
 *   EYE_MQTT_CERT_FILE, EYE_MQTT_KEY_FILE   client certificate and key (PEM) for mutual TLS
 *   EYE_MQTT_INSECURE=1      TLS without verifying the server certificate (testing only)
 */
#pragma once

#include <string>

namespace output {

/** Read the EYE_* environment variables and start the enabled outputs. Call once. */
void start();

/**
 * Hand one JSON message to every enabled output. Never blocks on the network. With HTTP on, it also counts
 * the message for /metrics and, at most every EYE_VPU_TELEMETRY_MS, reads the Myriad X telemetry over USB
 * (about 3 ms). Call it from the inference thread: that is what keeps those reads within the pipeline's life.
 */
void publish(const std::string &json);

} // namespace output
