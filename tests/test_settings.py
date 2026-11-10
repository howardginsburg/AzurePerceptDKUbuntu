"""Hardware-free tests of Device Settings. Run from the repository root: python -B -m unittest discover -s tests -v"""

from __future__ import annotations

import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from services.settings import configuration, server
from scripts.kit import config_guard as guard

ROOT = Path(__file__).resolve().parents[1]


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.controller = server.Controller(
            self.project, ROOT / "config.example" / "percept.env",
            state=self.project / "state", etc=self.project / "etc",
        )
        self.config = self.controller.config
        for name in ("share_file",):
            patcher = mock.patch.object(self.controller, name)
            patcher.start()
            self.addCleanup(patcher.stop)


class ConfigurationTests(Fixture):
    def test_every_setting_of_the_example_is_offered_with_its_default(self) -> None:
        self.assertEqual(self.config.values()["MOUTH_SPEED"], "165")
        self.assertEqual(self.config.values()["HDMI_MODE"], "console")
        self.assertEqual(self.config.values()["EYE_STRICT_MODEL"], "0")
        self.assertIn("PIP_INDEX_URL", self.config.defaults)
        self.assertIn("VOICE_ALERT_COOLDOWN", self.config.defaults)
        self.assertEqual(len(self.config.schema), len(self.config.defaults))

    def test_edits_keep_comments_and_other_lines_and_drop_duplicates(self) -> None:
        configuration.atomic_write(self.config.path, b"# note\nMOUTH_SPEED=170\nCUSTOM=value\nMOUTH_SPEED=180\n")
        self.assertEqual(self.config.render({"MOUTH_SPEED": "190", "EYE_MODEL": None}),
                         b"# note\nMOUTH_SPEED=190\nCUSTOM=value\n")
        self.config.save({"MOUTH_SPEED": None})
        self.assertEqual(self.config.values()["MOUTH_SPEED"], "165")
        self.assertIn("CUSTOM=value", self.config.text())

    def test_values_the_services_cant_use_are_refused(self) -> None:
        for key, value in [
            ("UNKNOWN", "x"), ("EYE_FPS", "0"), ("EYE_FPS", "2.5"), ("EYE_FPS", "nan"),
            ("VOICE_MIN_CONF", "1.1"), ("MOUTH_SINK", "speaker"), ("EYE_STDOUT", "true"),
            ("MOUTH_BT_VOLUME", "101"), ("MOUTH_VOICE", "one\nEYE_FPS=3"), ("MOUTH_VOICE", "${HOME}"),
            ("EYE_MODEL", "/etc/passwd"), ("EYE_MODEL", "/config/files/missing.onnx"),
            ("PROMETHEUS_RETENTION_SIZE", "0GB"), ("PROMETHEUS_RETENTION", "unlimited"),
            ("MQTT_URL", "http://broker"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(configuration.ControlError):
                self.config.validate({key: value})

    def test_valid_values_and_resets_pass(self) -> None:
        changes = {"EYE_FPS": "60", "VOICE_MIN_CONF": "0.5", "MOUTH_BT_VOLUME": "keep",
                   "HDMI_FEED": "raw", "MQTT_URL": "mqtts://broker.example:8883",
                   "VOICE_GAIN_DB": "-10", "EYE_MODEL": None}
        self.assertEqual(self.config.validate(changes), changes)

    def test_models_are_offered_and_an_xml_model_needs_its_bin(self) -> None:
        self.config.files.mkdir(parents=True)
        (self.config.files / "model.xml").write_text("<model/>")
        with self.assertRaisesRegex(configuration.ControlError, "matching .bin"):
            self.config.validate({"EYE_MODEL": "/config/files/model.xml"})
        (self.config.files / "model.bin").write_bytes(b"weights")
        self.config.validate({"EYE_MODEL": "/config/files/model.xml"})
        self.assertIn("/config/files/model.xml", [item["value"] for item in self.config.model_choices()])

    def test_file_names_stay_inside_config_files(self) -> None:
        self.assertEqual(self.config.file_path("labels-v2.txt"), self.config.files / "labels-v2.txt")
        for name in ["../model.onnx", "a/b.onnx", ".hidden.txt", "my model.zip"]:
            with self.subTest(name=name), self.assertRaises(configuration.ControlError):
                self.config.file_path(name)

    def test_a_change_recreates_only_what_it_belongs_to(self) -> None:
        with mock.patch.object(self.controller, "apply_configuration", return_value="") as apply:
            self.controller.save_settings({"revision": self.config.revision(), "changes": {"MOUTH_SPEED": "180"}})
        apply.assert_called_once_with(["MOUTH_SPEED"])
        self.assertEqual(self.config.values()["MOUTH_SPEED"], "180")
        self.assertTrue((self.project / ".settings-config-dirty").exists())

    def test_edits_made_from_an_old_copy_of_the_page_are_refused(self) -> None:
        revision = self.config.revision()
        self.config.save({"EYE_FPS": "15"})
        with self.assertRaisesRegex(configuration.ControlError, "changed elsewhere"):
            self.controller.save_settings({"revision": revision, "changes": {"EYE_FPS": "20"}})
        self.assertEqual(self.config.values()["EYE_FPS"], "15")

    def test_settings_that_fail_to_apply_are_put_back(self) -> None:
        self.config.save({"MOUTH_SPEED": "175"})
        original = self.config.text()
        with mock.patch.object(self.controller, "apply_configuration",
                               side_effect=[configuration.ControlError("compose failed"), ""]) as apply:
            with self.assertRaisesRegex(configuration.ControlError, "previous settings were restored"):
                self.controller.save_settings({"revision": self.config.revision(), "changes": {"MOUTH_SPEED": "200"}})
        self.assertEqual(self.config.text(), original)
        self.assertEqual(apply.call_count, 2)

    def test_the_preview_changes_nothing(self) -> None:
        self.controller.preview = True
        with self.assertRaisesRegex(configuration.ControlError, "read-only"):
            self.controller.save_settings({"revision": self.config.revision(), "changes": {"HDMI_MODE": "camera"}})
        self.assertFalse(self.config.path.exists())

    def test_uploads_replace_and_files_in_use_stay(self) -> None:
        self.controller.upload("labels.txt", io.BytesIO(b"cat\n"), 4)
        self.controller.upload("labels.txt", io.BytesIO(b"person\n"), 7)
        self.assertEqual((self.config.files / "labels.txt").read_bytes(), b"person\n")
        with self.assertRaisesRegex(configuration.ControlError, "cut short"):
            self.controller.upload("model.onnx", io.BytesIO(b"abc"), 10)
        self.assertFalse((self.config.files / "model.onnx").exists())
        self.config.save({"EYE_LABELS": "/config/files/labels.txt"})
        with self.assertRaisesRegex(configuration.ControlError, "selected in a setting"):
            self.controller.delete_file({"name": "labels.txt"})


class DeployGuardTests(Fixture):
    def manifest(self) -> str:
        return "\n".join(f"{digest}  {name}" for name, digest in guard.config_files(self.project).items())

    def test_a_deploy_cant_overwrite_changes_made_on_the_page(self) -> None:
        self.config.save({"EYE_FPS": "15"})
        self.config.mark_web_change()
        with self.assertRaisesRegex(ValueError, "settings pull"):
            guard.check_offer(self.project, "")
        guard.check_offer(self.project, self.manifest())
        old = self.manifest()
        self.config.files.mkdir(parents=True)
        (self.config.files / "uploaded.onnx").write_bytes(b"model")
        with self.assertRaises(ValueError):
            guard.check_offer(self.project, old)

    def test_a_change_during_a_deploy_is_noticed(self) -> None:
        before = guard.fingerprint(self.project)
        self.config.save({"MOUTH_SPEED": "180"})
        self.assertNotEqual(before, guard.fingerprint(self.project))

    def test_without_page_changes_deploy_works_as_before(self) -> None:
        self.config.save({"EYE_FPS": "15"})
        guard.check_offer(self.project, "a" * 64 + "  config/percept.env")


class NetworkTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.links = [{"name": "wlan0", "state": "UP", "addresses": ["192.168.1.10/24"]},
                      {"name": "eth0", "state": "UP", "addresses": []},
                      {"name": "eth1", "state": "DOWN", "addresses": []}]
        for name, value in [("interfaces", self.links), ("reload_network", None)]:
            patcher = mock.patch.object(self.controller, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = self.controller.etc / "systemd" / "network" / "10-percept-eth0.network"

    def test_static_addresses_are_checked(self) -> None:
        request = {"interface": "eth0", "mode": "static", "address": "192.168.1.50/24",
                   "gateway": "192.168.1.1", "dns": "192.168.1.1 1.1.1.1"}
        interface, files, check = self.controller.validate_network(request)
        self.assertEqual((interface, check), ("eth0", True))
        self.assertIn(b"Address=192.168.1.50/24\nGateway=192.168.1.1\nDNS=192.168.1.1\nDNS=1.1.1.1\n", files[self.target])
        for change in [{"gateway": "10.0.0.1"}, {"address": "not-an-address"}, {"dns": ""}, {"interface": "eth9"}]:
            with self.subTest(change=change), self.assertRaises(configuration.ControlError):
                self.controller.validate_network({**request, **change})

    def test_wifi_keeps_the_country_and_stores_a_psk(self) -> None:
        wifi = self.controller.etc / "wpa_supplicant" / "wpa_supplicant-wlan0.conf"
        wifi.parent.mkdir(parents=True)
        wifi.write_text('ctrl_interface=/run/wpa_supplicant\ncountry=US\nnetwork={\n    ssid="Old"\n    psk="old-password"\n}\n')
        _, files, _ = self.controller.validate_network(
            {"interface": "wlan0", "mode": "dhcp", "ssid": 'Lab "5G"', "password": "new-password"})
        text = files[wifi].decode()
        self.assertIn("country=US", text)
        self.assertNotIn("Old", text)
        self.assertNotIn("new-password", text)
        self.assertIn("ssid=" + 'Lab "5G"'.encode().hex(), text)
        self.assertEqual(text.count("network={"), 1)

    def test_settings_that_dont_connect_are_put_back(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_text("old")
        with mock.patch.object(self.controller, "wait_for_network",
                               side_effect=configuration.ControlError("no gateway")), \
                mock.patch.object(self.controller, "apply_configuration") as apply:
            with self.assertRaisesRegex(configuration.ControlError, "previous ones were restored"):
                self.controller.network_change({"interface": "eth0", "mode": "dhcp"})
        self.assertEqual(self.target.read_text(), "old")
        self.assertFalse((self.controller.state / "network-pending.json").exists())
        apply.assert_not_called()

    def test_settings_that_connect_are_kept(self) -> None:
        with mock.patch.object(self.controller, "wait_for_network"), \
                mock.patch.object(self.controller, "apply_configuration", return_value="") as apply:
            result = self.controller.network_change({"interface": "eth0", "mode": "dhcp"})
        self.assertIn("connected", result["message"])
        self.assertIn(b"DHCP=yes", self.target.read_bytes())
        apply.assert_called_once_with(["SYSTEM_NETWORK"])
        self.assertFalse((self.controller.state / "network-pending.json").exists())

    def test_an_interface_without_a_link_is_saved_without_a_check(self) -> None:
        with mock.patch.object(self.controller, "wait_for_network") as wait, \
                mock.patch.object(self.controller, "apply_configuration", return_value=""):
            result = self.controller.network_change({"interface": "eth1", "mode": "dhcp"})
        wait.assert_not_called()
        self.assertIn("no link", result["message"])

    def test_a_change_cut_short_by_a_reboot_is_undone_at_start(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_text("new")
        record = {"interface": "eth0", "previous": {str(self.target): None}}
        configuration.atomic_write(self.controller.state / "network-pending.json", json.dumps(record).encode())
        self.controller.rollback_network()
        self.assertFalse(self.target.exists())
        self.assertFalse((self.controller.state / "network-pending.json").exists())

    def test_ready_means_the_gateway_answers(self) -> None:
        with mock.patch.object(server, "run", side_effect=['[{"dst":"default","gateway":"192.168.1.1"}]', ""]) as command:
            self.assertTrue(self.controller.network_ready("wlan0"))
        self.assertEqual(command.call_args.args[0][-1], "192.168.1.1")
        with mock.patch.object(server, "run", return_value="[]"):
            self.assertFalse(self.controller.network_ready("wlan0"))


class HostTests(Fixture):
    def test_bad_host_names_and_power_actions_run_nothing(self) -> None:
        for action, body in [(self.controller.hostname, {"hostname": "-invalid"}),
                             (self.controller.hostname, {"hostname": "Upper"}),
                             (self.controller.power, {"action": "halt-and-catch-fire"})]:
            with self.subTest(body=body), mock.patch.object(server, "run") as command:
                with self.assertRaises(configuration.ControlError):
                    action(body)
                command.assert_not_called()

    def test_rename_updates_hosts_and_the_containers_that_use_the_name(self) -> None:
        hosts = self.controller.etc / "hosts"
        hosts.parent.mkdir(parents=True)
        hosts.write_text("127.0.0.1\tlocalhost\n127.0.1.1\tpercept-ubuntu\n")
        with mock.patch.object(server, "run") as command, \
                mock.patch.object(self.controller, "apply_configuration", return_value="") as apply:
            self.controller.hostname({"hostname": "lab-percept"})
        command.assert_called_once_with(["hostnamectl", "set-hostname", "lab-percept"])
        self.assertEqual(hosts.read_text(), "127.0.0.1\tlocalhost\n127.0.1.1\tlab-percept\n")
        apply.assert_called_once_with(["SYSTEM_HOSTNAME"])

    # mpv 0.34.1's output on this device when 1080p frame buffers didn't fit in CMA.
    NO_DISPLAY_MEMORY = [
        "[vo/drm] Cannot create dumb buffer: Cannot allocate memory", "[vo/drm] Cannot create framebuffer",
        "[vo/drm] Failed to set up buffers.", "Error opening/initializing the selected video_out (--vo) device.",
        "Video: no video", "No video or audio streams selected.", "Exiting... (Errors when loading file)",
    ]
    NO_STREAM = ["[ffmpeg] tcp: Connection to tcp://127.0.0.1:8554?timeout=0 failed: Connection refused",
                 "Failed to open rtsp://127.0.0.1:8554/resultTCP.", "Exiting... (Errors when loading file)"]

    def run_camera(self, plays: list[tuple[int, list[str]]], clock: list[float]) -> tuple[mock.MagicMock, ...]:
        status = self.project / "card0-HDMI-A-1" / "status"
        status.parent.mkdir(exist_ok=True)
        status.write_text("connected\n")
        with mock.patch.object(server.Path, "glob", return_value=[status]), \
                mock.patch.object(server, "rtsp_ready", return_value=True), \
                mock.patch.object(server, "free_display_memory") as free, \
                mock.patch.object(server, "run") as command, \
                mock.patch.object(server, "play", side_effect=plays) as player, \
                mock.patch.object(server.time, "monotonic", side_effect=clock), \
                mock.patch.object(server.time, "sleep") as sleep, \
                mock.patch.object(server, "camera_status", wraps=server.camera_status) as states:
            with self.assertRaises(SystemExit) as stopped:
                server.camera(self.controller)
        self.assertEqual(stopped.exception.code, server.STOPPED)
        self.assertEqual(free.call_count, len(plays))
        return command, player, sleep, states

    def test_camera_plays_the_selected_feed_at_720p_and_stops_after_three_display_errors(self) -> None:
        self.config.save({"HDMI_MODE": "camera", "HDMI_FEED": "raw"})
        command, player, sleep, states = self.run_camera([(2, self.NO_DISPLAY_MEMORY)] * 3, [0, 1, 10, 11, 20, 21])
        args = player.call_args.args[0]
        for expected in ["--vo=drm", "--no-audio", "--quiet", "--swapchain-depth=1", "--drm-device=/dev/dri/card0",
                         "--drm-connector=HDMI-A-1", "--drm-mode=1280x720", "rtsp://127.0.0.1:8554/rawTCP"]:
            self.assertIn(expected, args)
        self.assertEqual([call.args[0] for call in command.call_args_list], [["chvt", "7"], ["chvt", "1"]] * 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 2])
        self.assertEqual([call.args[1] for call in states.call_args_list][-1], "failed")
        with mock.patch.object(server, "run", return_value="inactive"), \
                mock.patch.object(server, "hdmi_connector", return_value=("card0", "HDMI-A-1")):
            camera = self.controller.display()["camera"]
        self.assertEqual(camera["state"], "failed")
        self.assertIn("Not enough display memory", camera["detail"])

    def test_camera_retries_a_missing_stream_and_plays_again_after_the_stream_ends(self) -> None:
        self.config.save({"HDMI_MODE": "camera"})
        _, player, sleep, states = self.run_camera(
            [(2, self.NO_STREAM), (0, ["Exiting... (End of file)"])] + [(2, self.NO_DISPLAY_MEMORY)] * 3,
            [0, 1, 100, 200, 300, 301, 310, 311, 320, 321])
        self.assertEqual(player.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 2, 2])
        self.assertEqual([call.args[1] for call in states.call_args_list][:7],
                         ["waiting", "playing", "retrying", "waiting", "playing", "waiting", "playing"])
        self.assertEqual(states.call_args_list[2].args[2], self.NO_STREAM[0])

    def test_camera_player_stops_for_good_in_console_mode(self) -> None:
        with self.assertRaises(SystemExit) as stopped:
            server.camera(self.controller)
        self.assertEqual(stopped.exception.code, server.STOPPED)
        unit = (ROOT / "scripts" / "host" / "percept-camera.service").read_text()
        for line in [f"RestartPreventExitStatus={server.STOPPED}", f"SuccessExitStatus={server.STOPPED}",
                     f"TTYPath=/dev/tty{server.CAMERA_VT}", "ExecStopPost=-/usr/bin/chvt 1"]:
            self.assertIn(line, unit)


class HttpTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        for name, value in [("interfaces", []), ("apply_configuration", "")]:
            patcher = mock.patch.object(self.controller, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = server.SettingsServer(("127.0.0.1", 0), self.controller)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        headers = {}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        is_json = response.getheader("Content-Type") == "application/json"
        connection.close()
        return response.status, json.loads(content) if is_json else content

    def wait(self, job: object) -> dict[str, object]:
        assert isinstance(job, dict)
        for _ in range(200):
            _, result = self.request("GET", "/api/jobs/" + job["job"])
            if isinstance(result, dict) and result["state"] != "running":
                return result
            time.sleep(0.01)
        self.fail("The job didn't finish.")

    def test_page_and_api_answer_without_sign_in(self) -> None:
        for path in ["/", "/app.js", "/style.css", "/health", "/api/config", "/api/overview", "/api/files"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(self.request("GET", "/missing")[0], 404)

    def test_bad_requests_get_a_clear_error(self) -> None:
        self.assertEqual(self.request("POST", "/api/config", b"{bad")[0], 400)
        self.assertEqual(self.request("POST", "/api/config", [])[0], 400)
        # Refused before the body is read: the answer must still arrive, not a connection reset.
        for _ in range(20):
            status, error = self.request("PUT", "/api/files/my%20model.zip", b"x" * 300000)
            self.assertEqual(status, 400)
            self.assertIn("letters, digits", error["error"])

    def test_changes_run_as_jobs_and_report_failures(self) -> None:
        status, job = self.request("POST", "/api/config",
                                   {"revision": self.config.revision(), "changes": {"MOUTH_SPEED": "180"}})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(job)["state"], "done")
        _, job = self.request("POST", "/api/config",
                              {"revision": self.config.revision(), "changes": {"MOUTH_SPEED": "9999"}})
        failed = self.wait(job)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("between", failed["error"])

    def test_upload_over_http(self) -> None:
        self.assertEqual(self.request("PUT", "/api/files/model.onnx", b"model")[0], 200)
        self.assertEqual(self.request("PUT", "/api/files/model.onnx", b"model v2")[0], 200)
        self.assertEqual((self.config.files / "model.onnx").read_bytes(), b"model v2")
        _, files = self.request("GET", "/api/files")
        self.assertEqual(files, [{"name": "model.onnx", "bytes": 8}])

    def test_the_preview_refuses_changes(self) -> None:
        self.controller.preview = True
        for _ in range(20):
            self.assertEqual(self.request("POST", "/api/power", {"action": "reboot"})[0], 409)
            self.assertEqual(self.request("PUT", "/api/files/model.onnx", b"m" * 300000)[0], 409)


if __name__ == "__main__":
    unittest.main()
