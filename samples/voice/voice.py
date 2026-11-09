#!/usr/bin/env python3
"""Azure Percept "voice" sample: voice commands answered from what the Eye sees.

  Ear  --/stream.wav-->  Vosk (fixed grammar)  -->  command  -->  reply via mouth /say
  Eye  --MQTT percept/eye/inferences-->  latest detections + presence (for "watch")
  Eye  --rtsp://eye:8554/result-->  ffmpeg  -->  /data/snapshots/*.jpg

Say the wake word and a command in one breath ("percept, what do you see"), or the wake word
alone, wait for the beep, then the command. Every command, reply and alert is published to
MQTT (<prefix>/commands, <prefix>/events) and shown on the dashboard (http://<device>:8080/).

  python3 voice.py                    run the sample
  python3 voice.py --test a.wav ...   recognise WAV files (any rate, 16-bit) and print the
                                      commands they trigger; nothing is spoken or published
"""
import audioop
import collections
import datetime
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vosk


def env(name, default):
    """A setting from the environment; unset or empty means the default."""
    value = os.environ.get(name, "")
    return value if value.strip() else default


EAR_URL = env("VOICE_EAR_URL", "http://ear:8000").rstrip("/")
MIC = env("VOICE_MIC", "mix")
GAIN_DB = float(env("VOICE_GAIN_DB", "12"))
MOUTH_URL = env("VOICE_MOUTH_URL", "http://mouth:8001").rstrip("/")
TTS_VOICE = env("VOICE_TTS_VOICE", "")
TAIL_LINE = float(env("VOICE_ECHO_TAIL", "0.4"))
TAIL_BT = float(env("VOICE_ECHO_TAIL_BT", "1.2"))
MQTT_URL = env("VOICE_MQTT_URL", "mqtt://mosquitto:1883")
MQTT_USERNAME = env("VOICE_MQTT_USERNAME", "")
MQTT_PASSWORD = env("VOICE_MQTT_PASSWORD", "")
MQTT_PASSWORD_FILE = env("VOICE_MQTT_PASSWORD_FILE", "")
MQTT_CA_FILE = env("VOICE_MQTT_CA_FILE", "")
MQTT_CERT_FILE = env("VOICE_MQTT_CERT_FILE", "")
MQTT_KEY_FILE = env("VOICE_MQTT_KEY_FILE", "")
MQTT_INSECURE = env("VOICE_MQTT_INSECURE", "0").lower() in ("1", "true", "yes", "on")
EYE_TOPIC = env("VOICE_EYE_TOPIC", "percept/eye/inferences")
PREFIX = env("VOICE_TOPIC_PREFIX", "percept/voice").rstrip("/")
RTSP_URL = env("VOICE_RTSP_URL", "rtsp://eye:8554/result")
WAKE = env("VOICE_WAKE_WORD", "percept").strip().lower()
if WAKE in ("none", "off"):
    WAKE = ""
WAKE_WINDOW = float(env("VOICE_WAKE_WINDOW", "5"))
MIN_CONF = float(env("VOICE_MIN_CONF", "0.5"))
STALE = float(env("VOICE_STALE_SECONDS", "2"))
PRESENT_FRAMES = int(env("VOICE_PRESENT_FRAMES", "3"))
ABSENT_SECONDS = float(env("VOICE_ABSENT_SECONDS", "3"))
ALERT_COOLDOWN = float(env("VOICE_ALERT_COOLDOWN", "30"))
WATCH_LABEL = env("VOICE_WATCH_LABEL", "person")
MAX_SNAPSHOTS = int(env("VOICE_MAX_SNAPSHOTS", "50"))
PREWARM = env("VOICE_PREWARM", "1") != "0"
PORT = int(env("VOICE_PORT", "8080"))
MODEL_DIR = env("VOICE_MODEL", "/opt/model")
DATA = env("VOICE_DATA", "/data")
SNAP_DIR = os.path.join(DATA, "snapshots")
RATE = 16000

# name: (phrases, handler name). Phrases must use words the speech model knows.
COMMANDS = {
    "see": ["what do you see", "what can you see", "what you see", "what are you seeing",
            "what do you see now"],
    "count": ["how many people", "how many people do you see", "how many people you see",
              "how many people are there", "count people"],
    "picture": ["take a picture", "take a photo"],
    "watch": ["watch for people", "start watching"],
    "unwatch": ["stop watching"],
    "help": ["help", "what can i say"],
}

NO_EYE = "I can't see anything right now. Is the eye running?"
SEE_NOTHING = "I don't see anything I recognize."
SEE_NOBODY = "I don't see anyone."
PIC_SAVED = "Picture saved."
PIC_FAILED = "Sorry, I couldn't take a picture."
WATCH_ON = "Okay, I'll tell you when someone arrives."
WATCH_OFF = "Okay, I've stopped watching."
SOMEONE_HERE = "Someone is here."
NOT_UNDERSTOOD = f"Sorry, I didn't understand. Say {WAKE}, help, for the list."
MICS_OFF = "Microphones off."
MICS_ON = "Microphones on."
MICS_ARE_OFF = "My microphones are off. Press the mute button to turn them on."

NUMBERS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


# ---------------------------------------------------------------- events (log, SSE, MQTT)
events = collections.deque(maxlen=200)
ev_lock = threading.Lock()
subscribers = set()
mqtt_client = None


def emit(kind, **fields):
    ev = {"ts": round(time.time(), 3), "type": kind, **fields}
    line = json.dumps(ev)
    print(line, flush=True)
    with ev_lock:
        events.append(ev)
        subs = list(subscribers)
    for q in subs:
        try:
            q.put_nowait(line)
        except queue.Full:
            pass
    if mqtt_client is not None:
        topic = f"{PREFIX}/commands" if kind == "command" else f"{PREFIX}/events"
        try:
            mqtt_client.publish(topic, line, qos=0)
        except Exception as e:  # noqa: BLE001 - never let MQTT break the sample
            print(f"mqtt publish failed: {e}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- what the Eye sees
state_lock = threading.Lock()
eye = {"mono": 0.0, "detections": [], "messages": 0}
presence = {"present": False, "consecutive": 0, "last_seen": 0.0, "last_alert": 0.0}
watching = threading.Event()


def on_inference(payload):
    try:
        msg = json.loads(payload)
        dets = []
        for d in msg.get("NEURAL_NETWORK") or []:
            conf = float(d.get("confidence", 0))
            if conf >= MIN_CONF:
                dets.append({"label": str(d.get("label", "?")), "confidence": round(conf, 3),
                             "bbox": d.get("bbox")})
    except (ValueError, TypeError, AttributeError):
        return
    now = time.monotonic()
    arrived = False
    with state_lock:
        eye.update(mono=now, detections=dets, messages=eye["messages"] + 1)
        if any(d["label"] == WATCH_LABEL for d in dets):
            presence["consecutive"] += 1
            presence["last_seen"] = now
            if not presence["present"] and presence["consecutive"] >= PRESENT_FRAMES:
                presence["present"] = arrived = True
        else:
            presence["consecutive"] = 0
    if arrived:
        emit("arrived", label=WATCH_LABEL)
        if watching.is_set() and now - presence["last_alert"] >= ALERT_COOLDOWN:
            presence["last_alert"] = now
            tasks.put(alert)


def housekeeping():
    while True:
        time.sleep(0.5)
        left = False
        with state_lock:
            if presence["present"] and time.monotonic() - presence["last_seen"] > ABSENT_SECONDS:
                presence["present"] = False
                left = True
        if left:
            emit("left", label=WATCH_LABEL)


def current_detections():
    """Detections from the latest Eye message, or None if there's no recent message."""
    with state_lock:
        if not eye["mono"] or time.monotonic() - eye["mono"] > STALE:
            return None
        return list(eye["detections"])


def plural(label, n):
    if n == 1:
        return label
    if label == "person":
        return "people"
    if re.search(r"(s|sh|ch|x|z)$", label):
        return label + "es"
    if re.search(r"[^aeiou]y$", label):
        return label[:-1] + "ies"
    return label + "s"


def amount(n, label):
    return f"{NUMBERS[n] if n < len(NUMBERS) else n} {plural(label, n)}"


def join_words(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def mqtt_password():
    if MQTT_PASSWORD_FILE:
        try:
            with open(MQTT_PASSWORD_FILE, encoding="utf-8") as f:
                return f.readline().rstrip("\r\n")
        except OSError as e:
            sys.exit(f"cannot read VOICE_MQTT_PASSWORD_FILE {MQTT_PASSWORD_FILE}: {e}")
    return MQTT_PASSWORD


def mqtt_start():
    """Subscribe to the Eye's inferences: mqtt://, mqtts://, ws:// or wss://, like the Eye's own client."""
    global mqtt_client
    import ssl
    import paho.mqtt.client as mqtt
    u = urllib.parse.urlparse(MQTT_URL)
    default_ports = {"mqtt": 1883, "mqtts": 8883, "ws": 80, "wss": 443}
    if u.scheme not in default_ports or not u.hostname:
        sys.exit(f"VOICE_MQTT_URL must be mqtt://, mqtts://, ws:// or wss://host[:port] (got {MQTT_URL})")
    websockets = u.scheme in ("ws", "wss")
    c = mqtt.Client(client_id=f"voice-{os.uname().nodename}"[:23],
                    transport="websockets" if websockets else "tcp")
    if websockets:
        c.ws_set_options(path=u.path or "/")
    user = MQTT_USERNAME or urllib.parse.unquote(u.username or "")
    if user:
        c.username_pw_set(user, mqtt_password() or urllib.parse.unquote(u.password or "") or None)
    if u.scheme in ("mqtts", "wss"):
        c.tls_set(ca_certs=MQTT_CA_FILE or None, certfile=MQTT_CERT_FILE or None,
                  keyfile=MQTT_KEY_FILE or None,
                  cert_reqs=ssl.CERT_NONE if MQTT_INSECURE else ssl.CERT_REQUIRED)
        if MQTT_INSECURE:
            c.tls_insecure_set(True)
            print("mqtt: VOICE_MQTT_INSECURE is set; the broker's certificate is NOT verified",
                  file=sys.stderr, flush=True)

    def on_connect(client, _userdata, _flags, rc):
        if rc == 0:
            client.subscribe(EYE_TOPIC, qos=0)
            print(f"mqtt connected to {u.hostname}; subscribed to {EYE_TOPIC}", flush=True)
        else:
            print(f"mqtt connect to {u.hostname} failed: {mqtt.connack_string(rc)}",
                  file=sys.stderr, flush=True)

    c.on_connect = on_connect
    c.on_message = lambda _c, _u, m: on_inference(m.payload)
    c.reconnect_delay_set(1, 30)
    c.connect_async(u.hostname, u.port or default_ports[u.scheme], keepalive=60)
    c.loop_start()
    mqtt_client = c


# ---------------------------------------------------------------- speaking (via mouth)
speaking = threading.Event()
quiet_after = [0.0]   # monotonic time when the mic may listen again


def mouth(path, body=b"", params=None):
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v})
    req = urllib.request.Request(f"{MOUTH_URL}{path}{'?' + q if q else ''}", data=body,
                                 method="POST")
    speaking.set()
    sink = None
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            sink = json.loads(r.read() or b"{}").get("sink")
    except (urllib.error.URLError, OSError, ValueError) as e:
        emit("error", what=f"mouth {path}", detail=str(e))
    finally:
        quiet_after[0] = time.monotonic() + (TAIL_BT if sink == "bluetooth" else TAIL_LINE)
        speaking.clear()
    return sink


def say(text):
    emit("reply", text=text)
    mouth("/say", text.encode(), {"voice": TTS_VOICE})


def tone(kind):
    mouth("/tone", params={"kind": kind})


# ---------------------------------------------------------------- snapshots
def snapshot():
    os.makedirs(SNAP_DIR, exist_ok=True)
    name = datetime.datetime.now().strftime("snap-%Y%m%d-%H%M%S.jpg")
    path = os.path.join(SNAP_DIR, name)
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", RTSP_URL,
           "-frames:v", "1", "-q:v", "3", "-y", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        ok = p.returncode == 0 and os.path.getsize(path) > 0
        err = p.stderr.strip()[-300:]
    except (subprocess.TimeoutExpired, OSError) as e:
        ok, err = False, str(e)
    if not ok:
        emit("error", what="snapshot", detail=err)
        return None
    snaps = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".jpg"))
    for old in snaps[:-MAX_SNAPSHOTS] if MAX_SNAPSHOTS > 0 else []:
        os.remove(os.path.join(SNAP_DIR, old))
    emit("snapshot", file=name)
    return name


# ---------------------------------------------------------------- command handlers
def do_see():
    dets = current_detections()
    if dets is None:
        return say(NO_EYE)
    if not dets:
        return say(SEE_NOTHING)
    counts = collections.Counter(d["label"] for d in dets)
    say("I see " + join_words([amount(n, label) for label, n in counts.most_common()]) + ".")


def do_count():
    dets = current_detections()
    if dets is None:
        return say(NO_EYE)
    n = sum(d["label"] == "person" for d in dets)
    say(SEE_NOBODY if n == 0 else f"I see {amount(n, 'person')}.")


def do_picture():
    tone("chime")
    say(PIC_SAVED if snapshot() else PIC_FAILED)


def do_watch():
    watching.set()
    presence["last_alert"] = 0.0
    emit("watching", on=True)
    say(WATCH_ON)


def do_unwatch():
    watching.clear()
    emit("watching", on=False)
    say(WATCH_OFF)


def help_text():
    phrases = [v[0] for v in COMMANDS.values() if v[0] != "help"]
    lead = f"Say {WAKE}, then: " if WAKE else "Say: "
    return lead + ", ".join(phrases[:-1]) + ", or " + phrases[-1] + "."


def do_help():
    say(help_text())


def do_not_understood():
    say(NOT_UNDERSTOOD)


def alert():
    tone("alert")
    name = snapshot()
    emit("alert", label=WATCH_LABEL, snapshot=name)
    say(SOMEONE_HERE)


def prewarm():
    """Have mouth render the fixed replies once, so they play without synthesis delay later.

    Mouth keeps rendered speech in its cache; already-cached phrases return immediately."""
    replies = [NO_EYE, SEE_NOTHING, SEE_NOBODY, PIC_SAVED, PIC_FAILED, WATCH_ON, WATCH_OFF,
               SOMEONE_HERE, NOT_UNDERSTOOD, MICS_OFF, MICS_ON, MICS_ARE_OFF,
               help_text()] + [f"I see {amount(n, 'person')}." for n in (1, 2, 3)]
    for text in replies:
        q = urllib.parse.urlencode({k: v for k, v in {"voice": TTS_VOICE, "render": "1"}.items() if v})
        req = urllib.request.Request(f"{MOUTH_URL}/say?{q}", data=text.encode(), method="POST")
        for attempt in range(10):
            try:
                urllib.request.urlopen(req, timeout=120).read()
                break
            except (urllib.error.URLError, OSError) as e:
                if attempt == 9:
                    print(f"prewarm stopped: {e}", file=sys.stderr, flush=True)
                    return
                time.sleep(10)
    print(f"prewarmed {len(replies)} replies", flush=True)


HANDLERS = {"see": do_see, "count": do_count, "picture": do_picture, "watch": do_watch,
            "unwatch": do_unwatch, "help": do_help}
tasks = queue.Queue()


def worker():
    while True:
        fn = tasks.get()
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep serving commands
            emit("error", what=getattr(fn, "__name__", "task"), detail=str(e))


def run_command(name, source):
    emit("command", name=name, source=source)
    tasks.put(HANDLERS[name])


# ---------------------------------------------------------------- Ear buttons
ptt_until = [0.0]  # monotonic time until which a command needs no wake word (after PTT)
mic_state = {"mute": None}


def ptt_listen():
    if mic_state["mute"]:
        return say(MICS_ARE_OFF)
    tone("beep")
    ptt_until[0] = time.monotonic() + WAKE_WINDOW


def watch_buttons():
    """Polls the Ear's button state. The SoM's lights can't be driven from Linux, so mute
    changes are announced instead; PTT works like saying the wake word."""
    presses = None
    while True:
        try:
            with urllib.request.urlopen(f"{EAR_URL}/levels", timeout=5) as r:
                d = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(3)
            continue
        mute = d.get("mute")
        if mute is not None and mic_state["mute"] is not None and mute != mic_state["mute"]:
            emit("mute", on=mute)
            tasks.put(lambda m=mute: say(MICS_OFF if m else MICS_ON))
        mic_state["mute"] = mute
        n = d.get("ptt_presses")
        if presses is not None and n is not None and n > presses:
            emit("ptt")
            tasks.put(ptt_listen)
        presses = n
        time.sleep(0.25)


# ---------------------------------------------------------------- recognition
def build_grammar(model):
    """Returns (grammar list, phrase -> command). Drops phrases with words the model lacks."""
    def missing(phrase):
        return [w for w in phrase.split() if model.vosk_model_find_word(w) < 0]

    if WAKE and missing(WAKE):
        sys.exit(f"wake word '{WAKE}' isn't in the speech model's vocabulary; choose another")
    lookup = {}
    for name, phrases in COMMANDS.items():
        for p in phrases:
            if missing(p):
                print(f"skipping phrase '{p}': not in vocabulary: {missing(p)}", file=sys.stderr)
            else:
                lookup[p] = name
    grammar = list(lookup)
    if WAKE:
        grammar += [WAKE] + [f"{WAKE} {p}" for p in lookup]
    return grammar + ["[unk]"], lookup


class Listener:
    """Turns recognised text into commands, handling the wake word."""

    def __init__(self, model, dispatch):
        self.grammar, self.lookup = build_grammar(model)
        self.rec = vosk.KaldiRecognizer(model, RATE, json.dumps(self.grammar))
        self.rec.SetWords(True)
        self.dispatch = dispatch
        self.armed_until = 0.0
        self.peak = 0

    def feed(self, pcm):
        self.peak = max(self.peak, audioop.max(pcm, 2))
        if self.rec.AcceptWaveform(pcm):
            self.result(json.loads(self.rec.Result()))

    def flush(self):
        self.result(json.loads(self.rec.FinalResult()))

    def reset(self):
        self.rec.Reset()
        self.peak = 0

    def result(self, res):
        text = res.get("text", "").strip()
        peak = self.peak
        self.peak = 0
        if not text:
            return
        conf = min((w.get("conf", 1.0) for w in res.get("result", [])), default=1.0)
        level = round(20 * math.log10(max(peak, 1) / 32768), 1)
        now = time.monotonic()
        if WAKE and text == WAKE:
            self.armed_until = now + WAKE_WINDOW
            self.dispatch("wake", text, conf, level)
            return
        phrase = text
        if WAKE:
            if text.startswith(WAKE + " "):
                phrase = text[len(WAKE) + 1:]
            elif now < max(self.armed_until, ptt_until[0]):
                self.armed_until = ptt_until[0] = 0.0
            else:
                return  # speech without the wake word: ignore
        name = self.lookup.get(phrase)
        if name:
            self.dispatch(name, text, conf, level)
        elif WAKE:
            # addressed to us, but not a command (Vosk returns [unk] for words outside the grammar)
            self.dispatch("unknown", text, conf, level)


def listen_forever(model):
    gain = 10 ** (GAIN_DB / 20)

    def dispatch(name, text, conf, level):
        emit("heard", text=text, conf=round(conf, 2), peak_dbfs=level)
        if name == "wake":
            threading.Thread(target=tone, args=("beep",), daemon=True).start()
        elif name == "unknown":
            emit("not_understood", text=text)
            tasks.put(do_not_understood)
        else:
            run_command(name, "voice")

    lst = Listener(model, dispatch)
    print(f"grammar: {len(lst.grammar)} phrases; wake word: {WAKE or '(none)'}", flush=True)
    url = f"{EAR_URL}/stream.wav?ch={MIC}"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                r.read(44)  # WAV header (16 kHz, 16-bit, mono)
                print(f"listening to {url}", flush=True)
                muted = False
                while True:
                    pcm = r.read(3200)  # 100 ms
                    if not pcm:
                        break
                    if speaking.is_set() or time.monotonic() < quiet_after[0]:
                        muted = True   # don't recognise our own voice
                        continue
                    if muted:
                        lst.reset()
                        muted = False
                    lst.feed(audioop.mul(pcm, 2, gain) if gain != 1 else pcm)
        except (urllib.error.URLError, OSError) as e:
            print(f"ear stream: {e}; retrying in 3 s", file=sys.stderr, flush=True)
        time.sleep(3)


def test_files(paths):
    model = vosk.Model(MODEL_DIR)
    status = 0
    for path in paths:
        heard = []
        lst = Listener(model, lambda n, t, c, l: heard.append((n, t, round(c, 2), l)))
        try:
            with wave.open(path, "rb") as w:
                if w.getsampwidth() != 2:
                    raise ValueError("needs 16-bit PCM")
                pcm = w.readframes(w.getnframes())
                if w.getnchannels() == 2:
                    pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
                if w.getframerate() != RATE:
                    pcm, _ = audioop.ratecv(pcm, 2, 1, w.getframerate(), RATE, None)
        except (OSError, ValueError, wave.Error) as e:
            print(f"{path}: {e}")
            status = 1
            continue
        pcm += b"\0" * RATE  # 0.5 s of silence so the last phrase ends
        for i in range(0, len(pcm), 3200):
            lst.feed(pcm[i:i + 3200])
        lst.flush()
        print(f"{path}: " + (", ".join(f"{n} ('{t}', conf {c}, peak {l} dBFS)"
                                       for n, t, c, l in heard) or "no command"))
    return status


# ---------------------------------------------------------------- dashboard
PAGE = """<!doctype html><meta charset=utf-8><title>Percept voice</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font:15px system-ui;margin:1.5em;max-width:60em}td,th{padding:.2em .6em;text-align:left}
#log{font:13px monospace;height:18em;overflow:auto;background:#f4f4f4;padding:.5em}
img{width:200px;margin:.2em;border:1px solid #ccc}button{margin:.2em}</style>
<h1>Percept voice sample</h1>
<p id=status>loading…</p>
<p>Try: <span id=cmds></span></p>
<h2>Eye sees</h2><table id=dets></table>
<h2>Events</h2><div id=log></div>
<h2>Snapshots</h2><div id=snaps></div>
<script>
const $=id=>document.getElementById(id);
function row(t,cells,th){const r=t.insertRow();cells.forEach(c=>{const d=document.createElement(th?'th':'td');d.textContent=c;r.appendChild(d)})}
async function state(){const s=await (await fetch('/state')).json();
 $('status').textContent=`Wake word: "${s.wake_word||'(none)'}" · watching: ${s.watching?'on':'off'} · person present: ${s.present?'yes':'no'} · eye: ${s.eye_age==null?'no data':s.eye_age+' s ago'}`;
 const t=$('dets');t.textContent='';row(t,['label','confidence'],1);(s.detections||[]).forEach(d=>row(t,[d.label,d.confidence]));
 if(!s.detections)row(t,['(no recent Eye data)','']);}
async function snaps(){const l=await (await fetch('/snapshots')).json();const d=$('snaps');d.textContent='';
 l.reverse().forEach(n=>{const a=document.createElement('a');a.href='/snapshots/'+n;const i=document.createElement('img');i.src=a.href;i.title=n;a.appendChild(i);d.appendChild(a)})}
fetch('/state').then(r=>r.json()).then(s=>{Object.entries(s.commands).forEach(([n,p])=>{const b=document.createElement('button');
 b.textContent=p;b.title='run "'+n+'" without speaking';b.onclick=()=>fetch('/command?name='+n,{method:'POST'});$('cmds').appendChild(b)})});
const es=new EventSource('/events');es.onmessage=e=>{const v=JSON.parse(e.data),l=$('log'),p=document.createElement('div');
 p.textContent=new Date(v.ts*1000).toLocaleTimeString()+'  '+v.type+'  '+JSON.stringify(Object.fromEntries(Object.entries(v).filter(([k])=>k!='ts'&&k!='type')));
 l.appendChild(p);l.scrollTop=l.scrollHeight;if(v.type=='snapshot')snaps();if(v.type=='watching')state()};
state();snaps();setInterval(state,1000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if url.path == "/state":
            dets = current_detections()
            with state_lock:
                age = round(time.monotonic() - eye["mono"], 1) if eye["mono"] else None
                present = presence["present"]
            return self.send(200, {"wake_word": WAKE, "watching": watching.is_set(),
                                   "present": present, "eye_age": age, "detections": dets,
                                   "commands": {n: p[0] for n, p in COMMANDS.items()}})
        if url.path == "/history":
            with ev_lock:
                return self.send(200, list(events))
        if url.path == "/snapshots":
            files = sorted(f for f in os.listdir(SNAP_DIR) if f.endswith(".jpg")) \
                if os.path.isdir(SNAP_DIR) else []
            return self.send(200, files)
        m = re.fullmatch(r"/snapshots/(snap-\d{8}-\d{6}\.jpg)", url.path)
        if m and os.path.exists(os.path.join(SNAP_DIR, m.group(1))):
            with open(os.path.join(SNAP_DIR, m.group(1)), "rb") as f:
                return self.send(200, f.read(), "image/jpeg")
        if url.path == "/events":
            return self.stream_events()
        self.send(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        name = urllib.parse.parse_qs(url.query).get("name", [""])[0]
        if url.path == "/command" and name in HANDLERS:
            run_command(name, "web")
            return self.send(202, {"queued": name})
        self.send(404, {"error": "unknown command" if url.path == "/command" else "not found"})

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue(maxsize=100)
        with ev_lock:
            backlog = [json.dumps(e) for e in list(events)[-20:]]
            subscribers.add(q)
        try:
            for line in backlog:
                self.wfile.write(f"data: {line}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    line = q.get(timeout=15)
                    self.wfile.write(f"data: {line}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with ev_lock:
                subscribers.discard(q)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        vosk.SetLogLevel(-1)
        sys.exit(test_files(sys.argv[2:]))
    vosk.SetLogLevel(-1)
    t0 = time.monotonic()
    model = vosk.Model(MODEL_DIR)
    print(f"speech model loaded in {time.monotonic() - t0:.1f} s", flush=True)
    mqtt_start()
    for fn in (housekeeping, worker, watch_buttons) + ((prewarm,) if PREWARM else ()):
        threading.Thread(target=fn, daemon=True).start()
    threading.Thread(target=listen_forever, args=(model,), daemon=True).start()
    srv = ThreadingHTTPServer(("", PORT), Handler)
    srv.daemon_threads = True
    print(f"dashboard on :{PORT}", flush=True)
    emit("started", wake_word=WAKE, mic=MIC)
    srv.serve_forever()


if __name__ == "__main__":
    main()
