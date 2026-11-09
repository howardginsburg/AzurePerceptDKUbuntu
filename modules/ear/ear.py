#!/usr/bin/env python3
"""Azure Percept Ear SoM service (standalone, no cloud).

Captures the Ear's 4-mic linear array through ALSA (`arecord`) and exposes:

  GET  /                 small web page: live audio player + mic level meters + button state
  GET  /stream.wav?ch=N  live 16 kHz 16-bit WAV; N = 0..3 (one mic), "mix" (average of the
                         four mics, mono) or "all" (4 ch)
  GET  /levels           JSON per-mic level in dBFS (latest 100 ms window) and button state

Buttons: the SoM reports one status byte through its USB HID interface (Linux input device,
ABS_MISC): bit 0 = Mute (the SoM's firmware silences the mics itself), bit 1 = PTT/PTS, which
toggles on each press. /levels reports "mute", "ptt" and "ptt_presses" (a counter, so a client
polling slowly still sees every press). Needs /dev/input readable (see ear.md).

Input only: sound output (the Ear's line-out or a Bluetooth speaker) is the mouth module's job.

Only the Python standard library and alsa-utils are used.

Environment (unset or empty = the default):
  EAR_DEVICE      ALSA capture device      (default hw:CARD=USB)
  EAR_PORT        HTTP port                (default 8000)
  EAR_LOG_LEVELS  seconds between JSON level lines on stdout, 0 = off (default 0)
  EAR_BUTTONS     input device of the SoM's buttons, none = off
                  (default /dev/input/by-id/usb-Microsoft_Corporation_Azure_Ear_SoM_USB-event-if04)
"""
import fcntl
import json
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from array import array
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

def env(name, default):
    """A setting from the environment; unset or empty means the default."""
    value = os.environ.get(name, "")
    return value if value.strip() else default


DEVICE = env("EAR_DEVICE", "hw:CARD=USB")
PORT = int(env("EAR_PORT", "8000"))
LOG_EVERY = float(env("EAR_LOG_LEVELS", "0"))
BUTTONS_DEV = env("EAR_BUTTONS", "/dev/input/by-id/usb-Microsoft_Corporation_Azure_Ear_SoM_USB-event-if04")
if BUTTONS_DEV.strip().lower() in ("none", "off"):
    BUTTONS_DEV = ""

RATE = 16000
HW_CH = 5          # 4 mics + 1 unused channel
MICS = 4
SAMPLE = 4         # S32_LE (24 significant bits, left-justified)
FRAME = HW_CH * SAMPLE
CHUNK_FRAMES = RATE // 10  # 100 ms

EV_ABS, ABS_MISC = 3, 0x28
EVENT = struct.Struct("llHHi")  # struct input_event: timeval, type, code, value
EVIOCGABS_MISC = (2 << 30) | (24 << 16) | (ord("E") << 8) | (0x40 + ABS_MISC)

levels = [None] * MICS
buttons = {"mute": None, "ptt": None, "ptt_presses": 0}
warned = set()
subscribers = set()
sub_lock = threading.Lock()


def mic16(raw, ch):
    """Top 16 bits of one channel as little-endian int16 bytes."""
    out = bytearray(len(raw) // FRAME * 2)
    out[0::2] = raw[ch * SAMPLE + 2::FRAME]
    out[1::2] = raw[ch * SAMPLE + 3::FRAME]
    return bytes(out)


def mics16(raw):
    """All four mics, interleaved int16."""
    n = len(raw) // FRAME
    out = bytearray(n * MICS * 2)
    for ch in range(MICS):
        out[ch * 2::MICS * 2] = raw[ch * SAMPLE + 2::FRAME]
        out[ch * 2 + 1::MICS * 2] = raw[ch * SAMPLE + 3::FRAME]
    return bytes(out)


def mix16(raw):
    """Average of the four mics as int16 bytes: speech from the front adds up, uncorrelated
    noise partly cancels."""
    a = array("i", raw)
    m = array("h", ((w + x + y + z) >> 18 for w, x, y, z in
                    zip(a[0::HW_CH], a[1::HW_CH], a[2::HW_CH], a[3::HW_CH])))
    return m.tobytes()


def set_buttons(value):
    mute, ptt = bool(value & 1), bool(value & 2)
    # A Mute press can also clear the PTT bit; count a press only when PTT changes alone.
    if buttons["ptt"] is not None and ptt != buttons["ptt"] and mute == buttons["mute"]:
        buttons["ptt_presses"] += 1
    if (mute, ptt) != (buttons["mute"], buttons["ptt"]):
        print(json.dumps({"ts": time.time(), "buttons": {"mute": mute, "ptt": ptt}}), flush=True)
    buttons["mute"], buttons["ptt"] = mute, ptt


def watch_buttons():
    while True:
        try:
            with open(BUTTONS_DEV, "rb") as f:
                time.sleep(0.3)  # opening starts the HID polling; let the first report arrive
                info = fcntl.ioctl(f, EVIOCGABS_MISC, bytes(24))
                set_buttons(struct.unpack("6i", info)[0])
                print(f"reading buttons from {BUTTONS_DEV}", flush=True)
                while True:
                    ev = f.read(EVENT.size)
                    if len(ev) < EVENT.size:
                        break
                    _, _, typ, code, value = EVENT.unpack(ev)
                    if typ == EV_ABS and code == ABS_MISC:
                        set_buttons(value)
        except OSError as e:
            if "buttons" not in warned:
                warned.add("buttons")
                print(f"buttons unavailable ({e}); retrying every 5 s", file=sys.stderr, flush=True)
        else:
            warned.discard("buttons")
        buttons["mute"] = buttons["ptt"] = None
        time.sleep(5)


def dbfs(pcm16):
    a = array("h", pcm16)
    if not a:
        return None
    rms = math.sqrt(math.sumprod(a, a) / len(a))
    return round(20 * math.log10(rms / 32768), 1) if rms > 0 else -120.0


def capture():
    cmd = ["arecord", "-q", "-D", DEVICE, "-f", "S32_LE", "-r", str(RATE),
           "-c", str(HW_CH), "-t", "raw"]
    while True:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        print(f"capturing from {DEVICE}", flush=True)
        last_log = 0.0
        while True:
            raw = proc.stdout.read(CHUNK_FRAMES * FRAME)
            if not raw:
                break
            for ch in range(MICS):
                levels[ch] = dbfs(mic16(raw, ch))
            with sub_lock:
                subs = list(subscribers)
            for q in subs:
                try:
                    q.put_nowait(raw)
                except queue.Full:
                    pass
            if LOG_EVERY and time.monotonic() - last_log >= LOG_EVERY:
                last_log = time.monotonic()
                print(json.dumps({"ts": time.time(), "dbfs": levels}), flush=True)
        proc.wait()
        print(f"arecord exited ({proc.returncode}); retrying in 3 s", file=sys.stderr, flush=True)
        time.sleep(3)


def wav_header(channels):
    size = 0xFFFFFFFF - 36  # unknown length (live stream)
    return (b"RIFF" + struct.pack("<I", size + 36) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, RATE, RATE * channels * 2, channels * 2, 16) +
            b"data" + struct.pack("<I", size))


PAGE = """<!doctype html><meta charset=utf-8><title>Percept Ear</title>
<style>body{font-family:sans-serif;margin:2em}.m{display:flex;align-items:center;gap:1em;margin:.3em 0}
.b{width:300px;height:14px;background:#ddd}.f{height:100%;background:#2a7}</style>
<h2>Azure Percept Ear</h2>
<p>Mic <select id=ch><option value=mix>mix (all 4)</option><option>0</option><option>1</option><option>2</option><option>3</option></select>
<button onclick="a.src='/stream.wav?ch='+ch.value;a.play()">Listen</button>
<button onclick="a.pause();a.src=''">Stop</button></p><audio id=a></audio><div id=lv></div>
<p id=bt></p>
<script>
const lv=document.getElementById('lv');
for(let i=0;i<4;i++)lv.innerHTML+=`<div class=m>mic ${i}<div class=b><div class=f id=f${i}></div></div><span id=t${i}></span></div>`;
setInterval(async()=>{const d=await (await fetch('/levels')).json();
d.dbfs.forEach((v,i)=>{if(v===null)return;document.getElementById('f'+i).style.width=Math.max(0,(v+90)/90*100)+'%';
document.getElementById('t'+i).textContent=v+' dBFS'});
document.getElementById('bt').textContent=d.mute===null?'Buttons: unavailable':
`Mute: ${d.mute?'ON (mics silenced by the SoM)':'off'} · PTT presses: ${d.ptt_presses}`},200);
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if url.path == "/levels":
            return self._send(200, json.dumps({"dbfs": levels, **buttons}).encode(), "application/json")
        if url.path == "/stream.wav":
            ch = parse_qs(url.query).get("ch", ["0"])[0]
            if ch == "all":
                conv, nch = mics16, MICS
            elif ch == "mix":
                conv, nch = mix16, 1
            elif ch in ("0", "1", "2", "3"):
                conv, nch = (lambda raw, c=int(ch): mic16(raw, c)), 1
            else:
                return self._send(400, b"ch must be 0-3, mix or all\n", "text/plain")
            q = queue.Queue(maxsize=50)
            with sub_lock:
                subscribers.add(q)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(wav_header(nch))
                while True:
                    self.wfile.write(conv(q.get()))
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with sub_lock:
                    subscribers.discard(q)
            return
        self._send(404, b"not found\n", "text/plain")


if __name__ == "__main__":
    threading.Thread(target=capture, daemon=True).start()
    if BUTTONS_DEV:
        threading.Thread(target=watch_buttons, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
