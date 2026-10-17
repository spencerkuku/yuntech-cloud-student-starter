import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
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
    def test_existing_user_data_packages_and_defines_consumer_service(self):
        source = (ROOT / "deploy" / "make_user_data.py").read_text(encoding="utf-8")
        self.assertIn('files["app/consumer.py"]', source)
        self.assertIn("inspection-consumer.service", source)
        self.assertIn("paho-mqtt==2.1.0", source)

    def test_deployment_transfers_credentials_restricts_permissions_and_checks_services(self):
        source = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("umask 077; tar -xf - -C /etc/inspection/iot", source)
        self.assertIn("chown inspection:inspection", source)
        self.assertIn(
            "chmod 600 /etc/inspection/iot/consumer.cert.pem "
            "/etc/inspection/iot/consumer.private.key",
            source,
        )
        self.assertIn("root:root:755", source)
        self.assertIn('systemctl enable inspection-consumer', source)
        self.assertIn("systemctl restart inspection-consumer", source)
        self.assertIn("systemctl is-active inspection inspection-consumer", source)
        self.assertIn('"IOT_ENDPOINT": endpoint', source)
        self.assertIn('"IOT_DEVICE_IDS": devices', source)
        self.assertIn("DEPLOY_APP_ENV_PATH", source)
        self.assertIn('f"g02-lin-d{i:02d}" for i in range(1, 11)', source)


if __name__ == "__main__":
    unittest.main()
