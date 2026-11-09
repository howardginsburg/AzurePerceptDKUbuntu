#!/usr/bin/env python3
"""Azure Percept "mouth": the device's audio output service (standalone, no cloud).

Plays sound on a connected Bluetooth speaker (A2DP, through the host's BlueALSA) or, when
none is connected, on the Ear SoM's 3.5 mm line-out. Sounds play one at a time and each
request returns when its sound has finished, so callers know when the room is quiet again.

  GET  /                   web page: current output, test sounds, Bluetooth speaker pairing
  GET  /status             JSON {"sink", "name", "device", "playing", "bt_admin", "voice", "voices", "tts_ready"}
  POST /play               body = WAV file
  POST /say               body = text (UTF-8), spoken by Piper (neural) or espeak-ng
                           ?voice=&speed=165; ?render=1 returns the WAV instead of playing
  POST /tone?kind=chime    built-in sounds: chime | beep | alert
  GET  /bt/devices         JSON list of the Bluetooth devices BlueZ knows
  POST /bt/scan?seconds=12 discover nearby devices, then list them
  POST /bt/connect?mac=    pair + trust + connect
  POST /bt/disconnect?mac= / POST /bt/forget?mac=

/play, /say and /tone return JSON {"sink", "name", "seconds"}.

Environment (unset or empty = the default):
  MOUTH_PORT      HTTP port                                 (default 8001)
  MOUTH_SINK      auto | bluetooth | line-out               (default auto)
  MOUTH_LINE_OUT  ALSA device of the line-out               (default plughw:CARD=USB)
  MOUTH_VOICE     a Piper voice in MOUTH_PIPER_DIR (e.g. en_US-kristin-medium), or an espeak-ng
                  voice (e.g. en-us, espeak:en-us+f3)       (default: the Piper voice in the image)
  MOUTH_SPEED     words per minute for espeak-ng; Piper scales its pace by speed/165 (default 165)
  MOUTH_PIPER_DIR folder of Piper voices (<name>.onnx + <name>.onnx.json) (default /opt/piper)
  MOUTH_CACHE_DIR rendered Piper speech is kept here, keyed by voice+speed+text (default /cache)
  MOUTH_CACHE_MAX files kept in the cache, oldest-used removed first; 0 = off (default 300)
  MOUTH_BT_LEAD   seconds of silence played before each sound on Bluetooth (default 0.3);
                  many speakers wake up late and would clip the start
  MOUTH_BT_IDLE_LEAD  silence instead, after MOUTH_BT_IDLE s without Bluetooth sound (default 1.0)
  MOUTH_BT_IDLE   see MOUTH_BT_IDLE_LEAD                    (default 4)
  MOUTH_BT_VOLUME volume % set when a speaker (re)connects; "keep" = leave it (default 100)
  MOUTH_BT_ADMIN  0 = hide and refuse scan/connect/disconnect/forget (default 1)
"""
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

def env(name, default):
    """A setting from the environment; unset or empty means the default."""
    value = os.environ.get(name, "")
    return value if value.strip() else default


PORT = int(env("MOUTH_PORT", "8001"))
SINK = env("MOUTH_SINK", "auto")
LINE_OUT = env("MOUTH_LINE_OUT", "plughw:CARD=USB")
PIPER_DIR = env("MOUTH_PIPER_DIR", "/opt/piper")
VOICE = env("MOUTH_VOICE", env("MOUTH_DEFAULT_VOICE", "en-us"))
SPEED = int(env("MOUTH_SPEED", "165"))
CACHE_DIR = env("MOUTH_CACHE_DIR", "/cache")
CACHE_MAX = int(env("MOUTH_CACHE_MAX", "300"))
BT_LEAD = float(env("MOUTH_BT_LEAD", "0.3"))
BT_IDLE_LEAD = float(env("MOUTH_BT_IDLE_LEAD", "1.0"))
BT_IDLE = float(env("MOUTH_BT_IDLE", "4"))
BT_ADMIN = env("MOUTH_BT_ADMIN", "1").strip() != "0"
BT_VOLUME = env("MOUTH_BT_VOLUME", "100").strip()
BT_SH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt.sh")

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
VOICE_RE = re.compile(r"^[A-Za-z0-9_:+\-]{1,60}$")
MAX_BODY = 50 * 1024 * 1024
TONE_RATE = 22050
TONES = {  # (Hz, seconds, gap after)
    "chime": [(880, 0.15, 0.03), (1320, 0.3, 0)],
    "beep": [(1000, 0.15, 0)],
    "alert": [(988, 0.12, 0.06)] * 3 + [(1319, 0.35, 0)],
}

play_lock = threading.Lock()
bt_lock = threading.Lock()
playing = threading.Event()
last_bt_end = [0.0]
warned = set()
bt_seen = set()
bt_seen_lock = threading.Lock()


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def warn_once(key, msg):
    if key not in warned:
        warned.add(key)
        print(msg, file=sys.stderr, flush=True)


def bt_sinks():
    """Connected Bluetooth A2DP playback PCMs as [(mac, name)], via the host's BlueALSA."""
    try:
        # generous timeout: Piper synthesis keeps every core busy
        r = subprocess.run(["bluealsa-aplay", "-L"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        warn_once("bluealsa", f"bluealsa-aplay failed: {e}")
        return []
    if r.returncode:
        warn_once("bluealsa", "BlueALSA unavailable (Bluetooth output disabled): " + r.stderr.strip())
        return []
    lines = r.stdout.splitlines()
    out = []
    for i, line in enumerate(lines):
        m = re.search(r"DEV=([0-9A-F:]{17}),PROFILE=a2dp", line)
        if m and i + 1 < len(lines) and "playback" in lines[i + 1]:
            out.append((m.group(1), lines[i + 1].strip().split(",")[0]))
    track_bt_connections(out)
    return out


def track_bt_connections(found):
    """Set MOUTH_BT_VOLUME once per new connection: BlueALSA may start a speaker well below full
    volume (seen: 32/127 after a reboot); within a connection the speaker's own buttons win."""
    with bt_seen_lock:
        new = [(mac, name) for mac, name in found if mac not in bt_seen]
        bt_seen.intersection_update(mac for mac, _ in found)
        bt_seen.update(mac for mac, _ in found)
    for mac, name in new:
        set_bt_volume(mac, name)


def set_bt_volume(mac, name):
    if BT_VOLUME.lower() in ("", "off", "keep"):
        return
    try:
        level = max(0, min(100, int(BT_VOLUME)))
        r = subprocess.run(["amixer", "-D", "bluealsa", "scontrols"],
                           capture_output=True, text=True, timeout=15)
        controls = re.findall(r"'(.+ - A2DP)'", r.stdout)
        # control names may be truncated, so match on a prefix; one control means it's ours
        match = [c for c in controls if c.startswith(name[:20])] or (controls if len(controls) == 1 else [])
        if not match:
            print(f"Bluetooth volume: no mixer control for {name} ({mac})", file=sys.stderr, flush=True)
            return
        r = subprocess.run(["amixer", "-q", "-D", "bluealsa", "sset", match[0], f"{level}%", "unmute"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode:
            print(f"Bluetooth volume: {r.stderr.strip()}", file=sys.stderr, flush=True)
        else:
            print(f"Bluetooth volume for {name} ({mac}) set to {level}%", flush=True)
    except (ValueError, OSError, subprocess.TimeoutExpired) as e:
        print(f"Bluetooth volume: {e}", file=sys.stderr, flush=True)


def watch_bt():
    """Polls for (re)connected speakers so the volume is set before the first sound."""
    while True:
        if SINK != "line-out":
            bt_sinks()
        time.sleep(10)


def current_sink():
    if SINK != "line-out":
        found = bt_sinks()
        if found:
            mac, name = found[0]
            return {"sink": "bluetooth", "name": name, "mac": mac,
                    "device": f"bluealsa:DEV={mac},PROFILE=a2dp"}
        if SINK == "bluetooth":
            return {"sink": "none", "name": "no Bluetooth speaker connected", "device": None}
    return {"sink": "line-out", "name": "Ear line-out", "device": LINE_OUT}


def wav_bytes(params, frames):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
    return buf.getvalue()


def lead_silence(data, seconds):
    try:
        with wave.open(io.BytesIO(data)) as w:
            p = w.getparams()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return data
    zero = b"\x80" if p.sampwidth == 1 else b"\x00"
    return wav_bytes(p, zero * (int(p.framerate * seconds) * p.nchannels * p.sampwidth) + frames)


def aplay(device, data):
    return subprocess.run(["aplay", "-q", "-D", device, "-"], input=data,
                          capture_output=True, timeout=600)


def play(data):
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise HttpError(400, "body must be a WAV file")
    with play_lock:
        s = current_sink()
        if s["device"] is None:
            raise HttpError(503, s["name"])
        t0 = time.monotonic()
        playing.set()
        try:
            if s["sink"] == "bluetooth":
                # The A2DP stream (and many speakers' amplifiers) wake up on the first audio and
                # drop it, so a sound after a pause gets a longer silent lead-in.
                idle = time.monotonic() - last_bt_end[0] > BT_IDLE
                r = aplay(s["device"], lead_silence(data, BT_IDLE_LEAD if idle else BT_LEAD))
                last_bt_end[0] = time.monotonic()
                if r.returncode and SINK == "auto":
                    print(f"Bluetooth playback failed ({r.stderr.decode().strip()}); using the line-out",
                          file=sys.stderr, flush=True)
                    s = {"sink": "line-out", "name": "Ear line-out", "device": LINE_OUT}
                    r = aplay(LINE_OUT, data)
            else:
                r = aplay(s["device"], data)
        finally:
            playing.clear()
        if r.returncode:
            raise HttpError(500, "aplay failed: " + r.stderr.decode(errors="replace").strip())
        return {"sink": s["sink"], "name": s["name"], "seconds": round(time.monotonic() - t0, 2)}


def piper_voices():
    try:
        return sorted(f[:-5] for f in os.listdir(PIPER_DIR) if f.endswith(".onnx"))
    except OSError:
        return []


try:
    from piper import PiperVoice, SynthesisConfig
except ImportError:
    PiperVoice = None
piper_lock = threading.Lock()   # loading and synthesis both use every core: one at a time
piper_loaded = {}


def engine(voice):
    """('piper', name) for an installed Piper voice, else ('espeak', espeak voice name)."""
    if voice.startswith("espeak:"):
        return "espeak", voice[7:]
    if PiperVoice and voice in piper_voices():
        return "piper", voice
    if re.match(r"^[a-z]{2,3}_[A-Z]{2}-", voice):
        raise HttpError(400, f"Piper voice {voice} is not installed")
    return "espeak", voice


def piper_voice(name):
    """Load a Piper voice once (about 12 s on the Percept); call with piper_lock held."""
    if name not in piper_loaded:
        t0 = time.monotonic()
        piper_loaded[name] = PiperVoice.load(os.path.join(PIPER_DIR, name + ".onnx"))
        print(f"Piper voice {name} loaded in {time.monotonic() - t0:.1f} s", flush=True)
    return piper_loaded[name]


def piper_tts(text, name, speed):
    buf = io.BytesIO()
    cfg = SynthesisConfig(length_scale=None if speed == 165 else 165 / speed)
    with piper_lock:
        try:
            with wave.open(buf, "wb") as w:
                piper_voice(name).synthesize_wav(text, w, syn_config=cfg)
        except Exception as e:  # onnxruntime/phonemizer errors have no common base class
            raise HttpError(500, f"Piper failed: {e}")
    return buf.getvalue()


def espeak_tts(text, voice, speed):
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        r = subprocess.run(["espeak-ng", "--stdin", "-v", voice, "-s", str(speed), "-w", f.name],
                           input=text.encode(), capture_output=True, timeout=60)
        if r.returncode:
            raise HttpError(500, "espeak-ng failed: " + r.stderr.decode(errors="replace").strip())
        return f.read()


def cache_put(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".wav")]
        if len(files) > CACHE_MAX:
            files.sort(key=os.path.getmtime)
            for old in files[:len(files) - CACHE_MAX]:
                os.remove(old)
    except OSError as e:
        warn_once("cache", f"speech cache disabled: {e}")


def tts(text, voice, speed):
    text = text.strip()
    if not text:
        raise HttpError(400, "no text")
    if len(text) > 2000:
        raise HttpError(400, "text longer than 2000 characters")
    if not VOICE_RE.match(voice):
        raise HttpError(400, "bad voice name")
    kind, name = engine(voice)
    if kind == "espeak":
        return espeak_tts(text, name, speed)
    path = None
    if CACHE_MAX > 0:
        key = hashlib.sha256(json.dumps([name, speed, text]).encode()).hexdigest()[:32]
        path = os.path.join(CACHE_DIR, key + ".wav")
        try:
            with open(path, "rb") as f:
                data = f.read()
            os.utime(path)
            return data
        except OSError:
            pass
    data = piper_tts(text, name, speed)
    if path:
        cache_put(path, data)
    return data


def preload():
    kind, name = engine(VOICE)
    if kind == "piper":
        with piper_lock:
            piper_voice(name)


def tone(kind):
    if kind not in TONES:
        raise HttpError(400, "kind must be one of: " + ", ".join(TONES))
    a = array("h")
    for hz, secs, gap in TONES[kind]:
        n = int(TONE_RATE * secs)
        for i in range(n):
            env = min(1.0, i / (TONE_RATE * 0.01), (n - i) / (TONE_RATE * 0.06))
            a.append(int(14000 * env * math.sin(2 * math.pi * hz * i / TONE_RATE)))
        a.extend([0] * int(TONE_RATE * gap))
    return wav_bytes((1, 2, TONE_RATE, len(a), "NONE", "not compressed"), a.tobytes())


def bt(args, timeout):
    """Run bt.sh; returns (ok, rows-or-message)."""
    with bt_lock:
        try:
            r = subprocess.run(["bash", BT_SH] + args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HttpError(504, "Bluetooth command timed out")
    if r.returncode:
        raise HttpError(500, (r.stderr or r.stdout).strip() or "Bluetooth command failed")
    lines = r.stdout.splitlines()
    if lines and lines[0].startswith("mac\t"):
        keys = lines[0].split("\t")
        return [dict(zip(keys, l.split("\t"))) for l in lines[1:] if l.count("\t") == len(keys) - 1]
    return {"result": r.stdout.strip()}


def mac_arg(q):
    mac = q.get("mac", [""])[0]
    if not MAC_RE.match(mac):
        raise HttpError(400, "mac must look like AA:BB:CC:DD:EE:FF")
    return mac.upper()


PAGE = """<!doctype html><meta charset=utf-8><title>Percept speaker and Bluetooth</title>
<style>body{font-family:sans-serif;margin:2em;max-width:52em}table{border-collapse:collapse;margin:.5em 0}
td,th{padding:.25em .7em;text-align:left;border-bottom:1px solid #ddd}#msg{color:#a50}small{color:#555}</style>
<h2>Azure Percept mouth: speaker and Bluetooth</h2>
<p>Output: <b id=sink>...</b> <span id=playing></span><br>Voice: <span id=voice></span></p>
<p><input id=text size=44 value="Hello from your Azure Percept."> <button onclick=say()>Say</button>
<button onclick="act('Playing',()=>post('/tone?kind=chime'))">Chime</button></p>
<p id=msg></p>
<div id=bt hidden><h3>Bluetooth speaker</h3>
<p><button onclick=scan()>Scan for speakers</button>
<label><input type=checkbox id=all onchange=show(last)> show non-audio devices</label></p>
<table id=devs></table>
<small>Put the speaker in pairing mode, Scan, then Connect. Connected speakers are trusted: they
reconnect by themselves when switched on, and the device reconnects them after a reboot.
Without a speaker, sound goes to the Ear's line-out.</small></div>
<script>
const $=id=>document.getElementById(id);let last=[];
async function post(u,body){const r=await fetch(u,{method:'POST',body});const t=await r.text();
 let j;try{j=JSON.parse(t)}catch(e){j={error:t}}if(!r.ok)throw new Error(j.error||r.status);return j}
async function act(label,f){$('msg').textContent=label+'...';
 try{await f();$('msg').textContent=''}catch(e){$('msg').textContent=e.message}status();devices()}
function say(){act('Speaking',()=>post('/say',$('text').value))}
function scan(){act('Scanning for 12 s',async()=>show(await post('/bt/scan?seconds=12')))}
function btn(label,u,what){const b=document.createElement('button');b.textContent=label;
 b.onclick=()=>act(what,()=>post(u));return b}
function show(list){last=list;const t=$('devs');t.replaceChildren();
 const rows=list.filter(d=>$('all').checked||d.audio=='yes'||d.paired=='yes')
  .sort((a,b)=>(b.connected=='yes')-(a.connected=='yes')||(b.paired=='yes')-(a.paired=='yes')||(+b.rssi||-999)-(+a.rssi||-999));
 const h=t.insertRow();['Name','Address','Signal','State',''].forEach(x=>{const c=document.createElement('th');c.textContent=x;h.appendChild(c)});
 if(!rows.length){const r=t.insertRow();r.insertCell().textContent='No speakers yet: put one in pairing mode and Scan.';return}
 for(const d of rows){const r=t.insertRow();const m=encodeURIComponent(d.mac);
  [d.name,d.mac,d.rssi=='-'?'':d.rssi+' dBm',d.connected=='yes'?'connected':d.paired=='yes'?'paired':''].forEach(x=>r.insertCell().textContent=x);
  const c=r.insertCell();
  c.appendChild(d.connected=='yes'?btn('Disconnect','/bt/disconnect?mac='+m,'Disconnecting'):btn('Connect','/bt/connect?mac='+m,'Connecting (up to a minute)'));
  if(d.paired=='yes')c.appendChild(btn('Forget','/bt/forget?mac='+m,'Forgetting'))}}
async function status(){const s=await (await fetch('/status')).json();
 $('sink').textContent=s.sink=='bluetooth'?'Bluetooth: '+s.name:s.name;
 $('playing').textContent=s.playing?'(playing)':'';$('bt').hidden=!s.bt_admin;
 if(!jumped&&location.hash=='#bt'&&s.bt_admin){jumped=1;$('bt').scrollIntoView()}
 $('voice').textContent=s.voice+(s.tts_ready?'':' (loading...)')}
let jumped=0;
async function devices(){if($('bt').hidden)return;const r=await fetch('/bt/devices');if(r.ok)show(await r.json())}
status().then(devices);setInterval(status,3000);
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n > MAX_BODY:
            raise HttpError(413, "body too large")
        return self.rfile.read(n)

    def _handle(self, method):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if method == "GET":
                if url.path == "/":
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if url.path == "/status":
                    s = current_sink()
                    s.update(playing=playing.is_set(), bt_admin=BT_ADMIN, voice=VOICE,
                             voices=piper_voices() if PiperVoice else [],
                             tts_ready=engine(VOICE)[0] == "espeak" or VOICE in piper_loaded)
                    return self._json(s)
                if url.path == "/bt/devices":
                    return self._json(bt(["devices"], 60))
            else:
                if url.path == "/play":
                    return self._json(play(self._body()))
                if url.path == "/say":
                    text = self._body().decode("utf-8", "replace") or q.get("text", [""])[0]
                    voice = q.get("voice", [VOICE])[0]
                    speed = max(80, min(400, int(q.get("speed", [SPEED])[0])))
                    data = tts(text, voice, speed)
                    if q.get("render", ["0"])[0] == "1":
                        return self._send(200, data, "audio/wav")
                    return self._json(play(data))
                if url.path == "/tone":
                    return self._json(play(tone(q.get("kind", ["chime"])[0])))
                if url.path.startswith("/bt/"):
                    if not BT_ADMIN:
                        raise HttpError(403, "Bluetooth administration is disabled (MOUTH_BT_ADMIN=0)")
                    if url.path == "/bt/scan":
                        secs = max(1, min(60, int(q.get("seconds", ["12"])[0])))
                        return self._json(bt(["scan", str(secs)], secs + 90))
                    if url.path == "/bt/connect":
                        return self._json(bt(["pair", mac_arg(q)], 180))
                    if url.path == "/bt/disconnect":
                        return self._json(bt(["disconnect", mac_arg(q)], 30))
                    if url.path == "/bt/forget":
                        return self._json(bt(["forget", mac_arg(q)], 30))
            raise HttpError(404, "not found")
        except HttpError as e:
            self._json({"error": str(e)}, e.code)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


if __name__ == "__main__":
    try:
        engine(VOICE)
    except HttpError as e:
        print(f"{e}; using espeak-ng en-us", file=sys.stderr, flush=True)
        VOICE = "en-us"
    server = ThreadingHTTPServer(("", PORT), Handler)
    print(f"listening on :{PORT}; voice: {VOICE}", flush=True)
    threading.Thread(target=preload, daemon=True).start()
    threading.Thread(target=lambda: print("output: {sink} ({name})".format(**current_sink()), flush=True),
                     daemon=True).start()
    threading.Thread(target=watch_bt, daemon=True).start()
    server.serve_forever()