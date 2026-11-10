"""Shared configuration validation for the device settings service."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class ControlError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


CHOICES = {
    "EYE_PARSER": ["", "ssd100", "ssd200", "yolo", "classification", "s1",
                   "openpose", "onnxssd", "faster-rcnn-resnet50", "unet", "ocr"],
    "EYE_RESOLUTION": ["native", "720p", "1080p"],
    "MOUTH_SINK": ["auto", "bluetooth", "line-out"],
    "VOICE_MIC": ["mix", "0", "1", "2", "3"],
    "HDMI_MODE": ["console", "camera"],
    "HDMI_FEED": ["raw", "result"],
    "HDMI_OUTPUT_MODE": ["1280x720", "1024x768", "720x576", "640x480"],
}
BOOLEANS = {
    "MQTT_INSECURE", "EYE_MQTT_RETAIN", "EYE_TIME_ALIGN", "EYE_STRICT_MODEL",
    "EYE_STDOUT", "MOUTH_BT_ADMIN", "VOICE_PREWARM",
}
INTEGERS = {
    "EYE_FPS": (1, 60), "EYE_MQTT_KEEPALIVE": (5, 3600),
    "EYE_OUTPUT_INTERVAL_MS": (0, 3600000), "EYE_VPU_TELEMETRY_MS": (0, 3600000),
    "EAR_LOG_LEVELS": (0, 86400), "MOUTH_SPEED": (80, 400),
    "MOUTH_CACHE_MAX": (0, 10000), "VOICE_GAIN_DB": (-20, 40),
    "VOICE_PRESENT_FRAMES": (1, 1000), "VOICE_MAX_SNAPSHOTS": (0, 10000),
}
NUMBERS = {
    "MOUTH_BT_LEAD": (0, 10), "MOUTH_BT_IDLE_LEAD": (0, 10),
    "MOUTH_BT_IDLE": (0, 3600), "VOICE_WAKE_WINDOW": (0.1, 60),
    "VOICE_ECHO_TAIL": (0, 30), "VOICE_ECHO_TAIL_BT": (0, 30),
    "VOICE_MIN_CONF": (0, 1), "VOICE_STALE_SECONDS": (0.1, 3600),
    "VOICE_ABSENT_SECONDS": (0.1, 3600), "VOICE_ALERT_COOLDOWN": (0, 86400),
}
FILE_KEYS = {
    "EYE_LABELS", "MQTT_PASSWORD_FILE", "MQTT_CA_FILE", "MQTT_CERT_FILE", "MQTT_KEY_FILE",
}
MODEL_EXTENSIONS = {".zip", ".blob", ".xml", ".onnx"}
ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$")


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if path.exists() and os.name != "nt":
            stat = path.stat()
            os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = ASSIGNMENT.fullmatch(line)
        if match:
            values[match[1]] = unquote(match[2])
    return values


def read_schema(example: Path) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    section = "General"
    comments: list[str] = []
    for line in example.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ---"):
            section = line.lstrip("# -").strip()
            comments = []
            continue
        match = ASSIGNMENT.fullmatch(line.removeprefix("#"))
        if match:
            key, default = match[1], unquote(match[2])
            # The example uses a descriptive placeholder for this generated value.
            if key == "EYE_MQTT_CLIENT_ID":
                default = ""
            field: dict[str, object] = {
                "key": key, "default": default, "section": section,
                "description": " ".join(comments),
            }
            if key in CHOICES:
                field["choices"] = CHOICES[key]
            elif key in BOOLEANS:
                field["choices"] = ["0", "1"]
            if key in INTEGERS:
                field.update(kind="integer", minimum=INTEGERS[key][0], maximum=INTEGERS[key][1])
            elif key in NUMBERS:
                field.update(kind="number", minimum=NUMBERS[key][0], maximum=NUMBERS[key][1])
            fields.append(field)
            comments = []
        elif line.startswith("#"):
            comments.append(line.lstrip("# "))
        else:
            comments = []
    return fields


def safe_filename(name: str) -> str:
    """File names a setting can hold unquoted: config/files has no subfolders."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name):
        raise ControlError("File names can only use letters, digits, '.', '_' and '-'.")
    return name


class Configuration:
    def __init__(self, project: Path, example: Path) -> None:
        self.project = project
        self.path = project / "config" / "percept.env"
        self.files = project / "config" / "files"
        self.schema = read_schema(example)
        self.defaults = {str(field["key"]): str(field["default"]) for field in self.schema}

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def revision(self) -> str:
        return hashlib.sha256(self.text().encode()).hexdigest()

    def values(self) -> dict[str, str]:
        configured = read_values(self.text())
        return {key: configured.get(key) or value for key, value in self.defaults.items()}

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": self.schema, "values": self.values(),
            "overrides": list(read_values(self.text())), "revision": self.revision(),
        }

    def require_revision(self, revision: object) -> None:
        if not isinstance(revision, str) or revision != self.revision():
            raise ControlError("Settings changed elsewhere. Reload before applying your edits.", 409)

    def file_path(self, name: str) -> Path:
        return self.files / safe_filename(name)

    def model_choices(self) -> list[dict[str, str]]:
        models = [
            {"value": "", "label": "Default SSD MobileNet v2 (COCO)"},
            {"value": "/app/data/ssd_mobilenet_v1_10.onnx", "label": "Built-in ONNX SSD (parser: onnxssd)"},
        ]
        if self.files.exists():
            for path in sorted(self.files.iterdir()):
                if path.is_file() and not path.is_symlink() and path.suffix.lower() in MODEL_EXTENSIONS:
                    models.append({"value": "/config/files/" + path.name, "label": path.name})
        return models

    def validate(self, changes: object) -> dict[str, str | None]:
        if not isinstance(changes, dict) or not changes:
            raise ControlError("Provide at least one setting to change.")
        result: dict[str, str | None] = {}
        for key, value in changes.items():
            if key not in self.defaults:
                raise ControlError(f"Unknown setting: {key}")
            if value is None:
                result[key] = None
                continue
            if not isinstance(value, str):
                raise ControlError(f"{key}: expected a string.")
            # Compose reads percept.env as a .env file: it would cut or expand these.
            if any(ord(c) < 32 or ord(c) == 127 for c in value) or any(c in value for c in "$`\"'\\"):
                raise ControlError(f"{key}: the settings file can't hold line breaks, quotes, backslashes, $ or `.")
            if value:
                if key in CHOICES and value not in CHOICES[key]:
                    raise ControlError(f"{key}: choose one of the listed values.")
                if key in BOOLEANS and value not in ("0", "1"):
                    raise ControlError(f"{key}: use 0 or 1.")
                if key in INTEGERS or key in NUMBERS:
                    low, high = (INTEGERS | NUMBERS)[key]
                    try:
                        number = float(value)
                    except ValueError as error:
                        raise ControlError(f"{key}: enter a number.") from error
                    if not math.isfinite(number) or not low <= number <= high:
                        raise ControlError(f"{key}: must be between {low} and {high}.")
                    if key in INTEGERS and not re.fullmatch(r"-?\d+", value):
                        raise ControlError(f"{key}: enter a whole number.")
                if key == "MOUTH_BT_VOLUME" and value != "keep":
                    if not value.isdigit() or not 0 <= int(value) <= 100:
                        raise ControlError("MOUTH_BT_VOLUME: use 0-100 or keep.")
                if key == "PROMETHEUS_RETENTION" and not re.fullmatch(r"[1-9]\d*(ms|s|m|h|d|w|y)", value):
                    raise ControlError("History duration must look like 30d or 12h.")
                if key == "PROMETHEUS_RETENTION_SIZE" and not re.fullmatch(r"[1-9]\d*(B|KB|MB|GB|TB)", value):
                    raise ControlError("History size must look like 500MB or 1GB.")
                if key == "MQTT_URL" and urlsplit(value).scheme not in {"mqtt", "mqtts", "ws", "wss"}:
                    raise ControlError("MQTT_URL: use mqtt, mqtts, ws or wss.")
                if key == "PIP_INDEX_URL" and urlsplit(value).scheme not in {"http", "https"}:
                    raise ControlError("PIP_INDEX_URL: use an HTTP(S) package index.")
                if key in FILE_KEYS or key == "EYE_MODEL":
                    self.validate_reference(key, value)
            result[key] = value
        return result

    def validate_reference(self, key: str, value: str) -> None:
        if key == "EYE_MODEL":
            url = urlsplit(value)
            if url.scheme in {"http", "https"} and url.hostname and url.path.endswith(".zip"):
                return
            if value == "/app/data/ssd_mobilenet_v1_10.onnx":
                return
        if not value.startswith("/config/files/"):
            raise ControlError(f"{key}: select an uploaded file inside /config/files.")
        path = self.file_path(value.removeprefix("/config/files/"))
        if not path.is_file():
            raise ControlError(f"{key}: upload {path.name} first.")
        if key == "EYE_MODEL":
            if path.suffix.lower() not in MODEL_EXTENSIONS:
                raise ControlError("Unsupported model format.")
            if path.suffix.lower() == ".xml" and not path.with_suffix(".bin").is_file():
                raise ControlError("An XML model requires its matching .bin file.")

    def render(self, changes: dict[str, str | None]) -> bytes:
        remaining = dict(changes)
        lines: list[str] = []
        for line in self.text().splitlines():
            match = ASSIGNMENT.fullmatch(line)
            if match and match[1] in changes:
                key = match[1]
                if key in remaining:
                    value = remaining.pop(key)
                    if value is not None:
                        lines.append(f"{key}={value}")
            else:
                lines.append(line)
        for key, value in remaining.items():
            if value is not None:
                lines.append(f"{key}={value}")
        return ("\n".join(lines) + "\n").encode()

    def mark_web_change(self) -> None:
        atomic_write(self.project / ".settings-config-dirty", b"Settings changed on the device.\n")

    def save(self, changes: dict[str, str | None]) -> None:
        atomic_write(self.path, self.render(changes), 0o640)
