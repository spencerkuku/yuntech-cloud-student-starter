import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load_module("w06_service", ROOT / "app" / "service.py")
consumer = load_module("w06_consumer", ROOT / "app" / "consumer.py")


class ConsumerClassificationTests(unittest.TestCase):
    event = {
        "event_id": "g02-lin-r1-d01-01",
        "device_id": "g02-lin-d01",
        "observed_at": "2026-10-17T07:00:00Z",
        "type": "status",
    }
    allowed = {"g02-lin-d01"}

    def test_http_statuses_map_to_required_results(self):
        for status, expected in (
            (201, "created"),
            (200, "duplicate"),
            (409, "rejected"),
            (503, "error"),
            (None, "error"),
        ):
            with self.subTest(status=status):
                event_id, result = consumer.process_payload(
                    json.dumps(self.event).encode(),
                    self.allowed,
                    "fixture-reporter-token",
                    post=lambda event, token: status,
                )
                self.assertEqual((event_id, result), (self.event["event_id"], expected))

    def test_disallowed_device_is_rejected_without_api_call(self):
        calls = []
        event = dict(self.event, device_id="g02-lin-d99")
        result = consumer.process_payload(
            json.dumps(event).encode(),
            self.allowed,
            "fixture-reporter-token",
            post=lambda event, token: calls.append((event, token)),
        )
        self.assertEqual(result, (self.event["event_id"], "rejected"))
        self.assertEqual(calls, [])

    def test_malformed_payload_is_rejected_without_api_call(self):
        calls = []
        result = consumer.process_payload(
            b"{not-json",
            self.allowed,
            "fixture-reporter-token",
            post=lambda event, token: calls.append((event, token)),
        )
        self.assertEqual(result, ("invalid", "rejected"))
        self.assertEqual(calls, [])

    def test_http_request_uses_reporter_bearer_without_logging_body(self):
        captured = {}

        class Response:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response()

        with mock.patch.object(consumer, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(consumer.send_event(self.event, "fixture-token"), 201)
        self.assertEqual(captured["url"], "http://127.0.0.1:8080/events")
        self.assertEqual(captured["authorization"], "Bearer fixture-token")
        self.assertEqual(captured["body"], self.event)
        self.assertEqual(captured["timeout"], 5)

    def test_transport_failure_and_http_error_are_classified(self):
        with mock.patch.object(consumer, "urlopen", side_effect=URLError("offline")):
            self.assertIsNone(consumer.send_event(self.event, "fixture-token"))
        error = HTTPError("http://127.0.0.1/events", 503, "unavailable", {}, None)
        with mock.patch.object(consumer, "urlopen", side_effect=error):
            self.assertEqual(consumer.send_event(self.event, "fixture-token"), 503)

    def test_one_line_result_contains_no_token_or_event_body(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            consumer.handle_payload(
                json.dumps(self.event).encode(),
                self.allowed,
                "fixture-token",
                post=lambda event, token: 201,
            )
        self.assertEqual(output.getvalue(), f"event_id={self.event['event_id']} result=created\n")
        self.assertNotIn("fixture-token", output.getvalue())
        self.assertNotIn("device_id", output.getvalue())


class FakeMqttClient:
    instance = None

    def __init__(self, **kwargs):
        self.options = kwargs
        self.callbacks = {}
        self.subscriptions = []
        self.tls_options = None
        self.insecure = None
        FakeMqttClient.instance = self

    def __setattr__(self, name, value):
        if name.startswith("on_"):
            self.callbacks[name] = value
        object.__setattr__(self, name, value)

    def tls_set(self, **kwargs):
        self.tls_options = kwargs

    def tls_insecure_set(self, value):
        self.insecure = value

    def connect(self, endpoint, port, keepalive):
        self.connection = (endpoint, port, keepalive)

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        self.callbacks["on_subscribe"](self, None, 1, [1], None)
        return 0, 1

    def loop_forever(self):
        self.callbacks["on_connect"](self, None, {}, 0, None)


class ConsumerMqttSetupTests(unittest.TestCase):
    def test_mqtt5_tls_and_qos1_subscription(self):
        environment = {
            "IOT_ENDPOINT": "endpoint.example.invalid",
            "IOT_TOPIC": "cxe/g02-lin/gw/events",
            "IOT_CLIENT_ID": "consumer-g02-lin",
            "IOT_DEVICE_IDS": ",".join(f"g02-lin-d{i:02d}" for i in range(1, 11)),
            "IOT_CA": "/fixture/ca.pem",
            "IOT_CERT": "/fixture/consumer.cert.pem",
            "IOT_KEY": "/fixture/consumer.private.key",
            "REPORTER_TOKEN": "fixture-reporter-token",
        }
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(consumer.mqtt, "Client", FakeMqttClient), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(consumer.main(), 0)
        client = FakeMqttClient.instance
        self.assertEqual(client.options["protocol"], consumer.mqtt.MQTTv5)
        self.assertEqual(client.options["callback_api_version"],
                         consumer.mqtt.CallbackAPIVersion.VERSION2)
        self.assertEqual(client.connection, ("endpoint.example.invalid", 8883, 60))
        self.assertEqual(client.subscriptions, [("cxe/g02-lin/gw/events", 1)])
        self.assertEqual(client.tls_options["cert_reqs"], consumer.ssl.CERT_REQUIRED)
        self.assertEqual(client.insecure, False)
        self.assertIn("subscribed rc=1", stdout.getvalue())
        self.assertNotIn("fixture-reporter-token", stdout.getvalue())


class DeviceFilterApiTests(unittest.TestCase):
    def setUp(self):
        self.tokens = mock.patch.dict(
            os.environ,
            {"REPORTER_TOKEN": "fixture-reporter", "OPERATOR_TOKEN": "fixture-operator"},
        )
        self.tokens.start()
        self.addCleanup(self.tokens.stop)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        version = Path(self.tempdir.name) / "version"
        version.write_text("b" * 40, encoding="utf-8")
        self.server = service.make_server(version, port=0)
        self.addCleanup(self.server.server_close)
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.shutdown)
        for index in range(55):
            event = {
                "event_id": f"filtered-{index:02d}",
                "device_id": "g02-lin-d01",
                "observed_at": f"2026-10-17T07:{index // 60:02d}:{index % 60:02d}Z",
                "type": "status",
                "received_at": f"2026-10-17T07:{index // 60:02d}:{index % 60:02d}Z",
            }
            self.server.store._mem[event["event_id"]] = event
        self.server.store._mem["other-device"] = {
            "event_id": "other-device",
            "device_id": "g02-lin-d02",
            "observed_at": "2026-10-17T07:59:00Z",
            "type": "status",
            "received_at": "2026-10-17T07:59:00Z",
        }

    def request(self, path, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_filter_requires_operator_and_returns_latest_50_newest_first(self):
        status, _ = self.request("/events?device_id=g02-lin-d01")
        self.assertEqual(status, 401)
        status, body = self.request(
            "/events?device_id=g02-lin-d01",
            token="fixture-operator",
        )
        self.assertEqual(status, 200)
        events = body["events"]
        self.assertEqual(len(events), 50)
        self.assertTrue(all(event["device_id"] == "g02-lin-d01" for event in events))
        self.assertEqual(events[0]["event_id"], "filtered-54")
        self.assertEqual(events[-1]["event_id"], "filtered-05")

    def test_unfiltered_request_keeps_original_latest_behavior(self):
        status, body = self.request("/events", token="fixture-operator")
        self.assertEqual(status, 200)
        self.assertEqual(body["events"][0]["event_id"], "other-device")
        self.assertEqual(len(body["events"]), 50)

    def test_display_page_encodes_filter_and_keeps_text_content(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="device-id"', page)
        self.assertIn("new URLSearchParams({ device_id: deviceId })", page)
        self.assertIn("cell.textContent", page)
        self.assertNotIn("innerHTML", page)


class ParameterizedDeviceQueryTests(unittest.TestCase):
    def test_device_filter_is_a_parameterized_received_at_query(self):
        class Cursor:
            def __init__(self):
                self.call = None

            def execute(self, query, parameters):
                self.call = (query, parameters)

            def fetchall(self):
                return []

            def close(self):
                pass

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

        store = object.__new__(service.EventStore)
        store._db_ok = True
        store._conn = Connection()
        result = store.latest(device_id="device' OR 1=1")
        query, parameters = store._conn.cursor_instance.call

        self.assertEqual(result, [])
        self.assertIn("WHERE device_id=%s", query)
        self.assertIn("ORDER BY received_at DESC", query)
        self.assertEqual(parameters, ("device' OR 1=1", 50))


class DeploymentIntegrationTests(unittest.TestCase):
    deploy_source = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")

    def remote_script(self, name):
        marker = "REMOTE_ENV" if name == "env" else "REMOTE_IOT_INSTALL"
        match = re.search(
            rf'{name.upper() if name == "env" else "REMOTE_IOT_INSTALL"}_SCRIPT="\$\('
            rf'cat <<\'{marker}\'\n(.*?)\n{marker}\n\)"',
            self.deploy_source,
            re.DOTALL,
        )
        if match is None:
            self.fail("expected remote deployment script is missing")
        return match.group(1)

    def make_remote_mocks(self, directory):
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        scripts = {
            "sudo": "#!/bin/sh\nexec \"$@\"\n",
            "chown": "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$MOCK_CHOWN_LOG\"\n",
            "stat": (
                "#!/bin/sh\n"
                "mode=$(/usr/bin/stat -c %a \"$3\") || exit\n"
                "case \"$3\" in\n"
                "  */etc/inspection) owner=root:root ;;\n"
                "  */etc/inspection/app.env) owner=root:root ;;\n"
                "  *) owner=inspection:inspection ;;\n"
                "esac\n"
                "printf '%s:%s:%s\\n' \"$owner\" \"$mode\" \"$3\" >> \"$MOCK_STAT_LOG\"\n"
                "printf '%s:%s\\n' \"$owner\" \"$mode\"\n"
            ),
            "tar": (
                "#!/usr/bin/python3\n"
                "import os, sys\n"
                "if os.environ.get('MOCK_TAR_FAIL'):\n"
                "    sys.stdout.buffer.write(b'partial env data')\n"
                "    raise SystemExit(9)\n"
                "os.execv('/usr/bin/tar', ['tar', *sys.argv[1:]])\n"
            ),
        }
        for name, content in scripts.items():
            path = bin_dir / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o700)
        return bin_dir

    def remote_command(self, script, sandbox):
        script = script.replace("/etc/inspection", str(sandbox / "etc" / "inspection"))
        result = subprocess.run(
            [
                "bash",
                "-c",
                'printf -v command "sudo sh -c %q" "$1"; printf "%s" "$command"',
                "remote-command-quote",
                script,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    @staticmethod
    def archive(files):
        result = io.BytesIO()
        with tarfile.open(fileobj=result, mode="w") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
        return result.getvalue()

    def run_remote(self, script, sandbox, archive, bin_dir, failure=False):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["MOCK_CHOWN_LOG"] = str(sandbox / "chown.log")
        env["MOCK_STAT_LOG"] = str(sandbox / "stat.log")
        if failure:
            env["MOCK_TAR_FAIL"] = "1"
        return subprocess.run(
            ["bash", "-c", self.remote_command(script, sandbox)],
            input=archive,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_existing_user_data_packages_and_defines_consumer_service(self):
        source = (ROOT / "deploy" / "make_user_data.py").read_text(encoding="utf-8")
        self.assertIn('files["app/consumer.py"]', source)
        self.assertIn("inspection-consumer.service", source)
        self.assertIn("paho-mqtt==2.1.0", source)

    def test_deployment_transfers_credentials_restricts_permissions_and_checks_services(self):
        source = self.deploy_source
        iot_script = self.remote_script("iot_install")
        self.assertRegex(
            iot_script,
            r"(?m)^chmod 600 \\\n"
            r"  /etc/inspection/iot/consumer\.cert\.pem \\\n"
            r"  /etc/inspection/iot/consumer\.private\.key \\\n"
            r"  /etc/inspection/iot/AmazonRootCA1\.pem$",
        )
        self.assertRegex(
            iot_script,
            r"(?m)^chown inspection:inspection \\\n"
            r"  /etc/inspection/iot/consumer\.cert\.pem \\\n"
            r"  /etc/inspection/iot/consumer\.private\.key \\\n"
            r"  /etc/inspection/iot/AmazonRootCA1\.pem$",
        )
        self.assertIn('"$REMOTE_IOT_INSTALL_COMMAND"', source)
        self.assertIn("root:root:755", source)
        self.assertIn('systemctl enable inspection-consumer', source)
        self.assertIn("systemctl restart inspection-consumer", source)
        self.assertIn("systemctl is-active inspection inspection-consumer", source)
        self.assertIn('"IOT_ENDPOINT": endpoint', source)
        self.assertIn('"IOT_DEVICE_IDS": devices', source)
        self.assertIn('stream.write("\\n".join(db_lines).rstrip() + "\\n")', source)
        self.assertIn('tmp="$(mktemp /etc/inspection/.app.env.XXXXXX)"', self.remote_script("env"))
        self.assertIn('trap \'rm -f "$tmp"\' EXIT', self.remote_script("env"))
        self.assertIn('mv -f "$tmp" /etc/inspection/app.env', self.remote_script("env"))
        self.assertIn("DEPLOY_APP_ENV_PATH", source)
        self.assertIn('f"g02-lin-d{i:02d}" for i in range(1, 11)', source)
        self.assertLess(source.index("REMOTE_INSTALL_COMMAND="), source.index("tar -C \"$IOT_DIR\""))
        self.assertLess(source.index("tar -C \"$IOT_DIR\""), source.index('tar -C "$DEPLOY_ENV_DIR"'))
        self.assertLess(source.index('tar -C "$DEPLOY_ENV_DIR"'), source.index("systemctl daemon-reload"))
        self.assertLess(source.index("printf -v REMOTE_ENV_COMMAND"), source.index("systemctl daemon-reload"))

    def test_remote_env_replaces_atomically_and_sets_root_600(self):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = Path(temporary)
            app_dir = sandbox / "etc" / "inspection"
            app_dir.mkdir(parents=True)
            app_env = app_dir / "app.env"
            app_env.write_text("W5_TOKEN=private-token\nDB_PASSWORD=db-secret\n", encoding="utf-8")
            app_env.chmod(0o600)
            bin_dir = self.make_remote_mocks(sandbox)
            archive = self.archive({
                "app.env": b"W5_TOKEN=new-token\nDB_PASSWORD=new-db-password\nIOT_KEY=private-key\n",
            })

            result = self.run_remote(self.remote_script("env"), sandbox, archive, bin_dir)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                app_env.read_bytes(),
                b"W5_TOKEN=new-token\nDB_PASSWORD=new-db-password\nIOT_KEY=private-key\n",
            )
            self.assertEqual(stat.S_IMODE(app_env.stat().st_mode), 0o600)
            self.assertEqual(list(app_dir.glob(".app.env.*")), [])
            log = (sandbox / "chown.log").read_text(encoding="utf-8")
            self.assertRegex(log, r"^root:root .*/\.app\.env\.[A-Za-z0-9]+$")
            output = result.stdout + result.stderr
            for secret in (b"new-token", b"new-db-password", b"private-key"):
                self.assertTrue(secret not in output, "remote command leaked a secret")

    def test_remote_env_failure_preserves_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = Path(temporary)
            app_dir = sandbox / "etc" / "inspection"
            app_dir.mkdir(parents=True)
            app_env = app_dir / "app.env"
            original = b"W5_TOKEN=original-token\nDB_PASSWORD=original-password\n"
            app_env.write_bytes(original)
            app_env.chmod(0o600)
            bin_dir = self.make_remote_mocks(sandbox)
            archive = self.archive({
                "app.env": b"W5_TOKEN=private-token\nDB_PASSWORD=database-password\nIOT_KEY=iot-private-key\n",
            })

            result = self.run_remote(
                self.remote_script("env"),
                sandbox,
                archive,
                bin_dir,
                failure=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(app_env.read_bytes(), original)
            self.assertEqual(list(app_dir.glob(".app.env.*")), [])
            self.assertFalse((sandbox / "chown.log").exists())
            output = result.stdout + result.stderr
            for secret in (b"private-token", b"database-password", b"iot-private-key"):
                self.assertTrue(secret not in output, "remote command leaked a secret")

    def test_remote_iot_command_applies_owner_and_mode_to_three_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = Path(temporary)
            inspection = sandbox / "etc" / "inspection"
            iot_dir = inspection / "iot"
            iot_dir.mkdir(parents=True)
            inspection.chmod(0o755)
            iot_dir.chmod(0o700)
            (inspection / "app.env").write_text("protected\n", encoding="utf-8")
            bin_dir = self.make_remote_mocks(sandbox)
            archive = self.archive({
                "consumer.cert.pem": b"certificate-secret",
                "consumer.private.key": b"private-key-secret",
                "AmazonRootCA1.pem": b"ca-cert",
            })

            result = self.run_remote(
                self.remote_script("iot_install"),
                sandbox,
                archive,
                bin_dir,
            )

            sanitized_error = result.stderr.decode("utf-8", errors="replace")
            for secret in ("certificate-secret", "private-key-secret"):
                sanitized_error = sanitized_error.replace(secret, "[redacted]")
            diagnostics = [
                sanitized_error,
                (sandbox / "stat.log").read_text(encoding="utf-8")
                if (sandbox / "stat.log").exists() else "",
                (sandbox / "chown.log").read_text(encoding="utf-8")
                if (sandbox / "chown.log").exists() else "",
            ]
            self.assertEqual(result.returncode, 0, "\n".join(diagnostics))
            for filename in ("consumer.cert.pem", "consumer.private.key", "AmazonRootCA1.pem"):
                path = iot_dir / filename
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertTrue(path.is_file())
            chown_lines = (sandbox / "chown.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(chown_lines), 1)
            self.assertTrue(chown_lines[0].startswith("inspection:inspection "))
            for filename in ("consumer.cert.pem", "consumer.private.key", "AmazonRootCA1.pem"):
                self.assertIn(str(iot_dir / filename), chown_lines[0])
            output = result.stdout + result.stderr
            for secret in (b"certificate-secret", b"private-key-secret"):
                self.assertTrue(secret not in output, "remote command leaked a secret")


if __name__ == "__main__":
    unittest.main()
