"""Exercise the real Bash configuration dispatcher with fake host commands."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def bash_path() -> str | None:
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidate = Path(git).parents[1] / "bin" / "bash.exe"
        if candidate.exists():
            return str(candidate)
    return shutil.which("bash")


def posix(path: Path) -> str:
    """C:/x as Git Bash's /c/x, so it can go in PATH."""
    text = path.as_posix()
    if os.name == "nt" and text[1:2] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


class StackConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        executable = bash_path()
        if executable is None:
            self.skipTest("Bash is not installed.")
        self.bash = executable
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.bin = self.project / "bin"
        self.bin.mkdir()
        (self.project / "config").mkdir()
        (self.project / "config" / "percept.env").write_text("MOUTH_SPEED=180\nHDMI_MODE=console\n")
        (self.project / "compose.yaml").write_text("services: {}\n")
        self.log = self.project / "commands.log"
        commands = {
            "docker": 'printf "docker %s\\n" "$*" >> "$PK_TEST_LOG"\n'
                      # The services that are on, as "docker compose config --services" lists them.
                      'if [ "$*" = "compose config --services" ]; then\n'
                      '  printf "%s\\n" ${PK_TEST_SERVICES:-eye ear mouth voice prometheus homepage go2rtc}\n'
                      'fi\n'
                      'exit "${PK_DOCKER_EXIT:-0}"\n',
            "systemctl": 'printf "systemctl %s\\n" "$*" >> "$PK_TEST_LOG"\n',
            "chvt": 'printf "chvt %s\\n" "$*" >> "$PK_TEST_LOG"\n',
            "getent": 'case "$2" in audio) echo "audio:x:29:";; input) echo "input:x:104:";; esac\n',
            "ip": 'echo "2: wlan0 inet 192.168.1.10/24 scope global wlan0"\n',
            # Windows can't chown; write_env's ownership is not what these tests check.
            "chown": "true\n",
            "install": 'for a; do last=$a; done\nmkdir -p "$last"\n',
        }
        for name, source in commands.items():
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + source, newline="\n")
            path.chmod(0o755)
        self.environment = {**os.environ, "PK_PROJECT": self.project.as_posix(), "PK_TEST_BIN": posix(self.bin),
                            "PK_TEST_LOG": self.log.as_posix(), "PK_CONTROL_LOCKED": "1"}

    def configure(self, *keys: str, fail: bool = False) -> subprocess.CompletedProcess[str]:
        env = {**self.environment}
        if fail:
            env["PK_DOCKER_EXIT"] = "1"
        # Git Bash puts its own /usr/bin first, so the fakes go first from inside Bash.
        wrapper = 'export PATH="$PK_TEST_BIN:$PATH"; exec bash "$@"'
        return subprocess.run([self.bash, "-c", wrapper, "bash", str(ROOT / "scripts" / "stack.sh"), "configure", *keys],
                              env=env, text=True, capture_output=True, timeout=20)

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.log.read_text()

    def test_audio_update_does_not_restart_the_demo(self) -> None:
        log = self.assert_success(self.configure("MOUTH_SPEED", "MOUTH_SINK"))
        self.assertIn("docker compose up -d --no-build --no-deps mouth", log)
        self.assertNotIn("grafana", log)
        self.assertNotIn("eye", log)
        self.assertEqual(log.count(" up "), 1)

    def test_shared_mqtt_only_updates_eye_and_voice(self) -> None:
        log = self.assert_success(self.configure("MQTT_EYE_TOPIC"))
        self.assertIn("up -d --no-build --no-deps eye voice", log)
        self.assertNotIn("mosquitto", log)
        self.assertNotIn("grafana", log)

    def test_monitoring_only_updates_prometheus(self) -> None:
        log = self.assert_success(self.configure("PROMETHEUS_RETENTION"))
        self.assertIn("up -d --no-build --no-deps prometheus", log)
        self.assertNotIn("grafana", log)

    def test_turned_off_services_are_not_started(self) -> None:
        # compose.override.yaml gave voice a profile that is never on, as for Azure IoT Hub.
        self.environment["PK_TEST_SERVICES"] = "eye ear mouth prometheus homepage go2rtc"
        result = self.configure("MQTT_EYE_TOPIC", "VOICE_WAKE_WORD")
        log = self.assert_success(result)
        self.assertIn("up -d --no-build --no-deps eye\n", log)
        self.assertNotIn("voice", log)
        self.assertIn("voice is turned off: not started", result.stdout)

        self.log.unlink()
        self.assert_success(self.configure("VOICE_WAKE_WORD"))
        self.assertNotIn(" up ", self.log.read_text())

    def test_console_mode_stops_only_the_camera_player(self) -> None:
        log = self.assert_success(self.configure("HDMI_MODE"))
        self.assertIn("systemctl -q disable --now percept-camera.service", log)
        self.assertNotIn("docker compose up", log)

    def test_camera_mode_restarts_the_player(self) -> None:
        (self.project / "config" / "percept.env").write_text("HDMI_MODE=camera\n")
        log = self.assert_success(self.configure("HDMI_MODE", "HDMI_FEED"))
        self.assertIn("systemctl -q enable percept-camera.service", log)
        self.assertEqual(log.count("systemctl restart percept-camera.service"), 1)
        self.assertNotIn("docker compose up", log)

    def test_failed_compose_validation_never_starts_services(self) -> None:
        result = self.configure("MOUTH_SPEED", fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(" up ", self.log.read_text())

    def test_strict_model_option_is_opt_in(self) -> None:
        script = 'exec() { printf "%s\\n" "$@"; }; export -f exec; bash "$1"'
        for enabled in ["0", "1"]:
            with self.subTest(enabled=enabled):
                result = subprocess.run(
                    [self.bash, "-c", script, "test", str(ROOT / "modules" / "eye" / "entrypoint.sh")],
                    env={**os.environ, "EYE_STRICT_MODEL": enabled}, text=True,
                    capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("-q" in result.stdout.splitlines(), enabled == "1")


if __name__ == "__main__":
    unittest.main()
