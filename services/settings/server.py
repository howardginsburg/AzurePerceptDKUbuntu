#!/usr/bin/env python3
"""Device Settings: web page and API, host controls, and the HDMI camera player. Standard library only."""

from __future__ import annotations

import argparse
import base64
import collections
from collections.abc import Callable, Generator
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

if __package__:
    from .configuration import Configuration, ControlError, atomic_write, read_values
else:
    from configuration import Configuration, ControlError, atomic_write, read_values

ASSETS = Path(__file__).resolve().parent
LIB = Path("/usr/local/lib/percept-kit")
STATE = Path("/var/lib/percept-kit")
PORT = 8002
NETWORK_TIMEOUT = 45
CAMERA_VT = "7"
CAMERA_STATUS = "camera-status.json"
# Exit status that tells systemd not to restart the camera player: HDMI mode is console, or the video
# output failed in a way that trying again can't fix.
STOPPED = 3
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
LOG = logging.getLogger("percept-settings")


class NetworkInterface(TypedDict):
    name: str
    state: str
    addresses: list[str]


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 60,
        env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise ControlError(f"{args[0]} timed out after {timeout} s.", 504) from error
    except OSError as error:
        raise ControlError(f"Cannot run {args[0]}: {error}", 503) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        raise ControlError(f"{args[0]} failed: {detail or result.returncode}", 503)
    return result.stdout.strip()


@contextmanager
def operation_lock(project: Path) -> Generator[None, None, None]:
    """The same lock stack.sh takes, so a deploy and a settings change never overlap."""
    path = project / ".control.lock"
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ControlError("A deploy or another settings change is running. Try again shortly.", 409) from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ControlError("A deploy or another settings change is running. Try again shortly.", 409) from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def string(body: dict[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ControlError(f"{key}: expected a non-empty string.")
    return value


def wpa_psk(password: str, ssid: str) -> str:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


def rtsp_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8554), timeout=2):
            return True
    except OSError:
        return False


def hdmi_connector() -> tuple[str, str] | None:
    """The DRM card and connector of a monitor plugged into HDMI, such as ("card0", "HDMI-A-1")."""
    for status in sorted(Path("/sys/class/drm").glob("card*-HDMI-A-*/status")):
        if status.read_text().strip() == "connected":
            card, connector = status.parent.name.split("-", 1)
            return card, connector
    return None


class Controller:
    def __init__(self, project: Path, example: Path, *, state: Path = STATE,
                 etc: Path = Path("/etc"), preview: bool = False) -> None:
        self.project = project
        self.config = Configuration(project, example)
        self.state = state
        self.etc = etc
        self.preview = preview

    def require_host(self) -> None:
        if self.preview:
            raise ControlError("Not available in the read-only preview.", 409)

    def share_file(self, path: Path) -> None:
        """config/ convention (stack.sh sync_upload): the device user and the modules' group 5000 can read it."""
        if os.name != "nt":
            os.chmod(path, 0o640)
            os.chown(path, self.project.stat().st_uid, 5000)

    def apply_configuration(self, keys: list[str]) -> str:
        return run(["bash", str(LIB / "stack.sh"), "configure", *keys], cwd=self.project, timeout=180,
                   env={**os.environ, "PK_PROJECT": str(self.project), "PK_CONTROL_LOCKED": "1"})

    # ------------------------------------------------------------ settings and files

    def save_settings(self, body: dict[str, object]) -> dict[str, object]:
        self.require_host()
        changes = self.config.validate(body.get("changes"))
        with operation_lock(self.project):
            self.config.require_revision(body.get("revision"))
            original = self.config.path.read_bytes() if self.config.path.exists() else None
            self.config.save(changes)
            self.share_file(self.config.path)
            try:
                output = self.apply_configuration(list(changes))
            except ControlError as error:
                if original is None:
                    self.config.path.unlink(missing_ok=True)
                else:
                    atomic_write(self.config.path, original, 0o640)
                try:
                    self.apply_configuration(list(changes))
                except ControlError as restore_error:
                    self.config.mark_web_change()
                    raise ControlError(f"{error} Restoring the previous settings also failed: {restore_error}", 503) from error
                raise ControlError(f"{error} The previous settings were restored.", 503) from error
            self.config.mark_web_change()
        return {"message": "Settings applied.", "output": output, "revision": self.config.revision()}

    def files(self) -> list[dict[str, object]]:
        if not self.config.files.exists():
            return []
        return [{"name": path.name, "bytes": path.stat().st_size}
                for path in sorted(self.config.files.iterdir())
                if path.is_file() and not path.name.startswith(".")]

    def upload(self, name: str, source: BinaryIO, size: int) -> dict[str, str]:
        self.require_host()
        destination = self.config.file_path(name)
        with operation_lock(self.project):
            self.config.files.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.project).free < size + 256 * 1024 * 1024:
                raise ControlError("Not enough disk space: uploads keep at least 256 MiB free.", 507)
            fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=self.config.files)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    remaining = size
                    while remaining:
                        chunk = source.read(min(1 << 16, remaining))
                        if not chunk:
                            raise ControlError("The upload was cut short.")
                        stream.write(chunk)
                        remaining -= len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.share_file(temporary)
                os.replace(temporary, destination)
                self.config.mark_web_change()
            finally:
                temporary.unlink(missing_ok=True)
        return {"message": f"Uploaded {destination.name}."}

    def delete_file(self, body: dict[str, object]) -> dict[str, str]:
        self.require_host()
        with operation_lock(self.project):
            path = self.config.file_path(string(body, "name"))
            if "/config/files/" + path.name in read_values(self.config.text()).values():
                raise ControlError(f"{path.name} is selected in a setting. Change that setting first.", 409)
            if not path.is_file():
                raise ControlError(f"No file named {path.name}.", 404)
            path.unlink()
            self.config.mark_web_change()
        return {"message": f"Removed {path.name}."}

    # ------------------------------------------------------------ Bluetooth (the mouth's API)

    def mouth(self, path: str, *, post: bool = False) -> object:
        self.require_host()
        request = Request("http://127.0.0.1:8001" + path, data=b"" if post else None)
        try:
            with urlopen(request, timeout=210 if post else 30) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]
            except (ValueError, KeyError, TypeError):
                pass
            raise ControlError(f"Speaker service: {detail}", 502) from error
        except (URLError, TimeoutError, ValueError) as error:
            raise ControlError(f"Speaker service unavailable: {error}", 503) from error

    def bluetooth(self, body: dict[str, object]) -> object:
        action = string(body, "action")
        if action == "scan":
            return self.mouth("/bt/scan?seconds=12", post=True)
        if action not in {"connect", "disconnect", "forget"}:
            raise ControlError("Bluetooth action must be scan, connect, disconnect or forget.")
        return self.mouth(f"/bt/{action}?" + urlencode({"mac": string(body, "mac")}), post=True)

    # ------------------------------------------------------------ containers

    def compose(self, args: list[str], timeout: int = 60) -> str:
        self.require_host()
        return run(["docker", "compose", *args], cwd=self.project, timeout=timeout)

    def services(self) -> list[dict[str, object]]:
        text = self.compose(["ps", "-a", "--format", "json"])
        if text.startswith("["):
            rows = json.loads(text)
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [{"service": row["Service"], "status": row.get("Status", ""), "health": row.get("Health", "")}
                for row in rows]

    def service_action(self, body: dict[str, object]) -> dict[str, str]:
        action, service = string(body, "action"), string(body, "service")
        if action not in {"start", "stop", "restart"}:
            raise ControlError("Action must be start, stop or restart.")
        with operation_lock(self.project):
            if service not in self.compose(["config", "--services"]).splitlines():
                raise ControlError(f"{service} is not part of the current configuration.", 409)
            if action == "start":
                output = self.compose(["up", "-d", "--no-build", "--no-deps", service], 120)
            else:
                output = self.compose([action, service], 120)
        return {"message": f"{service}: {action} done.", "output": output}

    def logs(self, service: str) -> dict[str, str]:
        return {"text": self.compose(["logs", "--no-color", "--tail", "80", service])}

    # ------------------------------------------------------------ device

    def display(self) -> dict[str, object]:
        values = self.config.values()
        info: dict[str, object] = {"mode": values["HDMI_MODE"], "output": values["HDMI_OUTPUT_MODE"]}
        if self.preview:
            return {**info, "connected": False, "player": "preview"}
        info["connected"] = hdmi_connector() is not None
        info["player"] = run(["systemctl", "show", "percept-camera.service", "--property=ActiveState", "--value"])
        status = self.state / CAMERA_STATUS
        if values["HDMI_MODE"] == "camera" and status.exists():
            info["camera"] = json.loads(status.read_text())
        return info

    def interfaces(self) -> list[NetworkInterface]:
        if self.preview:
            return []
        rows = json.loads(run(["ip", "-j", "address", "show"]))
        return [{"name": row["ifname"], "state": row.get("operstate", ""),
                 "addresses": [f"{a['local']}/{a['prefixlen']}" for a in row.get("addr_info", [])
                               if a.get("family") == "inet"]}
                for row in rows if row["ifname"] in {"eth0", "eth1", "wlan0"}]

    def overview(self) -> dict[str, object]:
        info: dict[str, object] = {
            "preview": self.preview,
            "hostname": "preview" if self.preview else socket.gethostname(),
            "disk_free": shutil.disk_usage(self.project).free,
            "web_changes": (self.project / ".settings-config-dirty").exists(),
        }
        for key, getter in (("display", self.display), ("interfaces", self.interfaces)):
            try:
                info[key] = getter()
            except (ControlError, OSError, ValueError) as error:
                info[key] = {"error": str(error)}
        return info

    def hostname(self, body: dict[str, object]) -> dict[str, str]:
        self.require_host()
        name = string(body, "hostname")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name):
            raise ControlError("Use 1-63 lowercase letters, digits and hyphens, not starting or ending with a hyphen.")
        with operation_lock(self.project):
            hosts = self.etc / "hosts"
            lines = [line for line in hosts.read_text().splitlines() if not re.match(r"\s*127\.0\.1\.1\s", line)]
            run(["hostnamectl", "set-hostname", name])
            atomic_write(hosts, ("\n".join([*lines, "127.0.1.1\t" + name]) + "\n").encode(), 0o644)
            self.apply_configuration(["SYSTEM_HOSTNAME"])
        return {"message": f"The device is now called {name}."}

    def validate_network(self, body: dict[str, object]) -> tuple[str, dict[Path, bytes], bool]:
        """The files for the change, and whether the interface has a link to check the change on."""
        interface = string(body, "interface")
        present = {row["name"]: row for row in self.interfaces()}
        if interface not in present:
            raise ControlError("Choose a network interface the device has.")
        content = f"[Match]\nName={interface}\n\n"
        if interface.startswith("eth"):
            content += "[Link]\nRequiredForOnline=no\n\n"
        content += "[Network]\n"
        mode = string(body, "mode")
        if mode == "dhcp":
            content += "DHCP=yes\nIPv6AcceptRA=yes\n"
        elif mode == "static":
            try:
                address = ipaddress.IPv4Interface(string(body, "address"))
                gateway = ipaddress.IPv4Address(string(body, "gateway"))
                dns = [ipaddress.ip_address(part) for part in string(body, "dns").split()]
            except ValueError as error:
                raise ControlError("Enter an IPv4 address with its prefix (192.168.1.50/24), a gateway, and DNS servers.") from error
            if gateway not in address.network:
                raise ControlError("The gateway must be in the same subnet as the address.")
            content += f"Address={address}\nGateway={gateway}\n" + "".join(f"DNS={server}\n" for server in dns)
        else:
            raise ControlError("Mode must be dhcp or static.")
        files = {self.etc / "systemd" / "network" / f"10-percept-{interface}.network": content.encode()}
        ssid = body.get("ssid")
        if interface == "wlan0" and isinstance(ssid, str) and ssid:
            password = string(body, "password")
            if not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(password) <= 63:
                raise ControlError("Wi-Fi needs an SSID of up to 32 bytes and a WPA2 password of 8-63 characters.")
            psk = wpa_psk(password, ssid)
            wifi = self.etc / "wpa_supplicant" / "wpa_supplicant-wlan0.conf"
            # Keep country= and the other global lines; replace the network block.
            header = re.sub(r"(?ms)^\s*network=\{.*?^\s*\}\s*$\n?", "", wifi.read_text()) if wifi.exists() else \
                "ctrl_interface=DIR=/run/wpa_supplicant GROUP=0\nupdate_config=0\n"
            block = f"network={{\n    ssid={ssid.encode().hex()}\n    psk={psk}\n}}\n"
            files[wifi] = (header.rstrip("\n") + "\n" + block).encode()
        return interface, files, present[interface]["state"] == "UP"

    def network_change(self, body: dict[str, object]) -> dict[str, str]:
        self.require_host()
        with operation_lock(self.project):
            interface, files, check = self.validate_network(body)
            previous = {str(path): base64.b64encode(path.read_bytes()).decode() if path.exists() else None
                        for path in files}
            atomic_write(self.state / "network-pending.json",
                         json.dumps({"interface": interface, "previous": previous}).encode())
            try:
                for path, content in files.items():
                    atomic_write(path, content, 0o600 if path.suffix == ".conf" else 0o644)
                self.reload_network(interface)
                if check:
                    self.wait_for_network(interface)
            except (ControlError, OSError, ValueError) as error:
                self.rollback_network()
                raise ControlError(f"The new settings didn't connect, so the previous ones were restored: {error}", 503) from error
            (self.state / "network-pending.json").unlink()
            self.apply_configuration(["SYSTEM_NETWORK"])
        if not check:
            return {"message": f"Saved. {interface} has no link now: the settings apply when it connects."}
        return {"message": f"{interface} is connected with the new settings."}

    def reload_network(self, interface: str) -> None:
        if interface == "wlan0":
            run(["systemctl", "restart", "wpa_supplicant@wlan0.service"], timeout=30)
        run(["networkctl", "reload"])
        run(["networkctl", "reconfigure", interface], timeout=30)

    def network_ready(self, interface: str) -> bool:
        routes = json.loads(run(["ip", "-j", "-4", "route", "show", "default", "dev", interface]) or "[]")
        gateway = next((route["gateway"] for route in routes if route.get("gateway")), None)
        if gateway is None:
            return False
        try:
            run(["ping", "-c", "1", "-W", "2", "-I", interface, gateway], timeout=5)
        except ControlError:
            return False
        return True

    def wait_for_network(self, interface: str) -> None:
        time.sleep(5)  # let the previous address and route go first
        deadline = time.monotonic() + NETWORK_TIMEOUT
        while time.monotonic() < deadline:
            if self.network_ready(interface):
                return
            time.sleep(2)
        raise ControlError(f"{interface} couldn't reach its gateway within {NETWORK_TIMEOUT} s.", 503)

    def rollback_network(self) -> None:
        """Puts back the files saved before a network change. At start-up, this finishes a change a crash or reboot cut short."""
        path = self.state / "network-pending.json"
        if not path.exists():
            return
        record = json.loads(path.read_text())
        for name, previous in record["previous"].items():
            target = Path(name)
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, base64.b64decode(previous), 0o600 if target.suffix == ".conf" else 0o644)
        self.reload_network(record["interface"])
        path.unlink()
        LOG.warning("Restored the previous network settings of %s", record["interface"])

    def power(self, body: dict[str, object]) -> dict[str, str]:
        self.require_host()
        action = string(body, "action")
        if action not in {"reboot", "poweroff"}:
            raise ControlError("Power action must be reboot or poweroff.")
        run(["systemd-run", "--quiet", "--collect", "--on-active=3", "/usr/bin/systemctl", action])
        return {"message": ("Rebooting" if action == "reboot" else "Shutting down") + " in 3 seconds."}


class Jobs:
    """Changes run in the background, one at a time; the page polls for the result."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, dict[str, object]] = {}

    def submit(self, action: Callable[[], object]) -> dict[str, str]:
        with self.lock:
            if any(job["state"] == "running" for job in self.items.values()):
                raise ControlError("Another change is still running. Wait for it to finish.", 409)
            while len(self.items) >= 12:
                del self.items[next(iter(self.items))]
            job_id = secrets.token_hex(8)
            self.items[job_id] = {"state": "running"}

        def worker() -> None:
            try:
                result: dict[str, object] = {"state": "done", "result": action()}
            except (ControlError, OSError, ValueError) as error:
                LOG.exception("Settings change failed")
                result = {"state": "failed", "error": str(error)}
            with self.lock:
                self.items[job_id] = result

        threading.Thread(target=worker, daemon=True).start()
        return {"job": job_id}

    def get(self, job_id: str) -> dict[str, object]:
        with self.lock:
            if job_id not in self.items:
                raise ControlError("Unknown job: reload the page to see the device's state.", 404)
            return dict(self.items[job_id])


class SettingsServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], controller: Controller) -> None:
        self.controller = controller
        self.jobs = Jobs()
        super().__init__(address, Handler)


class Reader:
    """The request body, counted: an error answer first reads what the client still sends, or the client
    may get a connection reset instead of the answer."""

    def __init__(self, stream: BinaryIO, size: int) -> None:
        self.stream = stream
        self.left = size

    def read(self, size: int) -> bytes:
        data = self.stream.read(min(size, self.left))
        self.left -= len(data)
        return data


class Handler(BaseHTTPRequestHandler):
    server: SettingsServer
    reader: Reader

    def send(self, value: object, status: int = 200, content_type: str = "application/json") -> None:
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, object]:
        try:
            value = json.loads(self.reader.read(self.reader.left) or b"{}")
        except (ValueError, UnicodeDecodeError) as error:
            raise ControlError("The request isn't valid JSON.") from error
        if not isinstance(value, dict):
            raise ControlError("Expected a JSON object.")
        return value

    def dispatch(self) -> None:
        path = urlsplit(self.path).path
        controller = self.server.controller
        if self.command == "GET":
            if path in STATIC:
                name, kind = STATIC[path]
                self.send((ASSETS / name).read_bytes(), content_type=kind)
            elif path == "/health":
                self.send({"status": "ok"})
            elif path == "/api/config":
                self.send({**controller.config.snapshot(), "models": controller.config.model_choices()})
            elif path == "/api/overview":
                self.send(controller.overview())
            elif path == "/api/files":
                self.send(controller.files())
            elif path == "/api/bluetooth":
                self.send({"devices": controller.mouth("/bt/devices"), "audio": controller.mouth("/status")})
            elif path == "/api/services":
                self.send(controller.services())
            elif path.startswith("/api/logs/"):
                self.send(controller.logs(unquote(path.removeprefix("/api/logs/"))))
            elif path.startswith("/api/jobs/"):
                self.send(self.server.jobs.get(path.removeprefix("/api/jobs/")))
            else:
                raise ControlError("Not found.", 404)
        elif self.command == "PUT" and path.startswith("/api/files/"):
            self.send(controller.upload(unquote(path.removeprefix("/api/files/")), self.reader, self.reader.left))
        elif self.command == "POST":
            body = self.body()
            actions: dict[str, Callable[[], object]] = {
                "/api/config": lambda: controller.save_settings(body),
                "/api/files/delete": lambda: controller.delete_file(body),
                "/api/bluetooth": lambda: controller.bluetooth(body),
                "/api/services": lambda: controller.service_action(body),
                "/api/hostname": lambda: controller.hostname(body),
                "/api/network": lambda: controller.network_change(body),
                "/api/power": lambda: controller.power(body),
            }
            if path not in actions:
                raise ControlError("Not found.", 404)
            controller.require_host()
            self.send(self.server.jobs.submit(actions[path]), 202)
        else:
            raise ControlError("Not found.", 404)

    def handle_request(self) -> None:
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size, self.close_connection = 0, True
        self.reader = Reader(self.rfile, size)
        try:
            self.dispatch()
        except ControlError as error:
            self.fail(str(error), error.status)
        except (OSError, ValueError, KeyError) as error:
            LOG.exception("Request failed")
            self.fail(f"Request failed: {error}", 500)

    def fail(self, message: str, status: int) -> None:
        while self.reader.left and self.reader.read(1 << 16):
            pass
        self.send({"error": message}, status)

    def do_GET(self) -> None:
        self.handle_request()

    def do_POST(self) -> None:
        self.handle_request()

    def do_PUT(self) -> None:
        self.handle_request()

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("%s %s", self.client_address[0], fmt % args)


def free_display_memory() -> None:
    """Frees the clean page cache, which fills most of CMA, so the display driver can take frame buffers
    from it. Measured on the device: CmaFree went from 2 MB to 22 MB; mpv needs 7 MB at 1280x720."""
    os.sync()
    Path("/proc/sys/vm/drop_caches").write_text("3")
    Path("/proc/sys/vm/compact_memory").write_text("1")


def camera_status(controller: Controller, state: str, detail: str = "") -> None:
    """What the camera player is doing, for the Settings page."""
    atomic_write(controller.state / CAMERA_STATUS,
                 json.dumps({"state": state, "detail": detail, "time": time.time()}).encode(), 0o644)


def play(args: list[str]) -> tuple[int, list[str]]:
    """Runs mpv, copying its output to the journal. Returns its exit status and its last lines."""
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    tail: collections.deque[str] = collections.deque(maxlen=10)
    if process.stdout is not None:
        for line in process.stdout:
            if line.strip():
                LOG.info("mpv: %s", line.rstrip())
                tail.append(line.strip())
    return process.wait(), list(tail)


def video_output_failed(lines: list[str]) -> str | None:
    """Why mpv couldn't draw on the monitor, or None if it got that far."""
    if any("Cannot create dumb buffer" in line for line in lines):
        # The DRM driver's frame buffers come from CMA, most of which the GPU driver reserves at boot.
        return "Not enough display memory for this output mode: choose a smaller one."
    for line in lines:
        if line.startswith("[vo/") or "video_out" in line:
            return line
    return None


def summary(lines: list[str], code: int) -> str:
    """The line of mpv's output that best says why it stopped."""
    for line in lines:
        if re.search(r"fail|error|cannot", line, re.IGNORECASE) and not line.startswith("Exiting..."):
            return line
    return lines[-1] if lines else f"mpv exit status {code}"


def camera(controller: Controller) -> None:
    """Plays the Eye's stream on HDMI with mpv, drawing straight to DRM on tty7. Waits for a monitor and the
    stream, plays again when the stream ends (the Eye restarted), and gives up after three video output
    errors in a row."""
    values = controller.config.values()
    if values["HDMI_MODE"] != "camera":
        LOG.info("HDMI mode is console: the camera player isn't needed")
        sys.exit(STOPPED)
    delay, display_failures = 5, 0
    while True:
        camera_status(controller, "waiting", "for a monitor on HDMI and for the Eye's video")
        while (display := hdmi_connector()) is None or not rtsp_ready():
            time.sleep(5)
        card, connector = display
        free_display_memory()
        run(["chvt", CAMERA_VT])
        camera_status(controller, "playing")
        started = time.monotonic()
        code, lines = play([
            "mpv", "--no-config", "--vo=drm", "--profile=sw-fast,low-latency", "--no-audio", "--quiet",
            "--input-terminal=no", "--input-default-bindings=no", "--osc=no", "--osd-level=0",
            # Two frame buffers rather than four: display memory is scarce.
            "--swapchain-depth=1",
            "--drm-device=/dev/dri/" + card, "--drm-connector=" + connector,
            "--drm-mode=" + values["HDMI_OUTPUT_MODE"], "--rtsp-transport=tcp",
            "rtsp://127.0.0.1:8554/" + values["HDMI_FEED"] + "TCP",
        ])
        if time.monotonic() - started > 30:
            LOG.info("The Eye's stream ended (mpv exit status %d): playing again", code)
            delay, display_failures = 5, 0
            continue
        run(["chvt", "1"])
        reason = video_output_failed(lines)
        if reason:
            display_failures += 1
            if display_failures >= 3:
                LOG.error("Camera stopped: %s", reason)
                camera_status(controller, "failed", reason)
                sys.exit(STOPPED)
            LOG.warning("Couldn't show the video (%s): trying again", reason)
            camera_status(controller, "retrying", reason)
            time.sleep(2)
            continue
        reason = summary(lines, code)
        LOG.warning("Couldn't play the Eye's stream (%s): trying again in %d s", reason, delay)
        camera_status(controller, "retrying", reason)
        time.sleep(delay)
        delay = min(delay * 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="the stack's folder (~/percept-kit on the device)")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--preview", action="store_true",
                        help="read-only preview on 127.0.0.1, with the example settings (for working on the page)")
    parser.add_argument("--camera", action="store_true", help="run the HDMI camera player")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    example = args.project / "config.example" / "percept.env"
    preview_directory = tempfile.TemporaryDirectory(prefix="percept-preview-") if args.preview else None
    project = Path(preview_directory.name) if preview_directory else args.project
    controller = Controller(project, example, preview=args.preview)
    if args.camera:
        camera(controller)
        return
    if not args.preview and (controller.state / "network-pending.json").exists():
        try:
            with operation_lock(project):
                controller.rollback_network()
        except (ControlError, OSError, ValueError):
            LOG.exception("Couldn't restore the network settings of an interrupted change")
    server = SettingsServer(("127.0.0.1" if args.preview else "0.0.0.0", args.port), controller)
    if args.preview:
        print(f"Read-only preview: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
