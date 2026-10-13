import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR / "app"))
import service  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures")


class W05ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        version_file = Path(cls.temp_dir.name) / "version"
        version_file.write_text("a" * 40, encoding="utf-8")
        env_file = Path(cls.temp_dir.name) / "app.env"
        env_file.write_text("REPORTER_TOKEN=reporter-secret\nOPERATOR_TOKEN=operator-secret\n", encoding="utf-8")
        cls.server = service.make_server(version_file, port=0, env_file=env_file)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp_dir.cleanup()

    def request(self, method, path, body=None, token=None, content_type="application/json"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = content_type
            body = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def fixture(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def test_health_reports_auth_configuration(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["auth_configured"])
        self.assertFalse(body["db_configured"])

    def test_unauthenticated_request_is_rejected_before_body_validation(self):
        status, body = self.request("POST", "/events", body=self.fixture("invalid-event-no-timezone.json"))
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "unauthorized"})

    def test_reporter_can_create_fixture_event(self):
        event = self.fixture("valid-event.json")
        event["event_id"] = "g02-report-success"
        status, body = self.request("POST", "/events", body=event, token="reporter-secret")
        self.assertEqual(status, 201)
        self.assertEqual(body["event_id"], "g02-report-success")
        self.assertIn("received_at", body)

    def test_operator_cannot_create_event(self):
        event = self.fixture("invalid-event-field.json")
        event["type"] = "status"
        status, body = self.request("POST", "/events", body=event, token="operator-secret")
        self.assertEqual(status, 403)
        self.assertEqual(body, {"error": "forbidden"})

    def test_invalid_fixtures_report_their_fields(self):
        status, body = self.request("POST", "/events", body=self.fixture("invalid-event-no-timezone.json"), token="reporter-secret")
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "observed_at")
        status, body = self.request("POST", "/events", body=self.fixture("invalid-event-field.json"), token="reporter-secret")
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "type")

    def test_duplicate_event_is_conflict(self):
        event = self.fixture("valid-event.json")
        event["event_id"] = "g02-duplicate"
        status, body = self.request("POST", "/events", body=event, token="reporter-secret")
        self.assertEqual(status, 201)
        status, body = self.request("POST", "/events", body=event, token="reporter-secret")
        self.assertEqual(status, 200)
        self.assertEqual(body["event_id"], "g02-duplicate")
        changed = dict(event, note="changed")
        status, body = self.request("POST", "/events", body=changed, token="reporter-secret")
        self.assertEqual(status, 409)
        self.assertEqual(body["field"], "event_id")

    def test_operator_can_list_and_get_event(self):
        event = self.fixture("valid-event.json")
        self.request("POST", "/events", body=event, token="reporter-secret")
        status, body = self.request("GET", "/events", token="operator-secret")
        self.assertEqual(status, 200)
        self.assertIn("g02-spencerku-0001", [event["event_id"] for event in body["events"]])
        status, body = self.request("GET", "/events/g02-spencerku-0001", token="operator-secret")
        self.assertEqual(status, 200)
        self.assertEqual(body["event_id"], "g02-spencerku-0001")

    def test_reporter_cannot_list_events(self):
        status, body = self.request("GET", "/events", token="reporter-secret")
        self.assertEqual(status, 403)
        self.assertEqual(body, {"error": "forbidden"})

    def test_content_type_and_body_limit(self):
        event = self.fixture("valid-event.json")
        event["event_id"] = "g02-large"
        status, body = self.request("POST", "/events", body=event, token="reporter-secret", content_type="text/plain")
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "Content-Type")
        raw = b"{" + b"a" * 4096
        status, body = self.request("POST", "/events", body=raw, token="reporter-secret")
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "body")
        exact_event = self.fixture("valid-event.json")
        exact_event["event_id"] = "g02-exact-4096"
        exact_body = json.dumps(exact_event).encode("utf-8")
        exact_body += b" " * (4096 - len(exact_body))
        status, body = self.request("POST", "/events", body=exact_body, token="reporter-secret")
        self.assertEqual(len(exact_body), 4096)
        self.assertEqual(status, 201)
        self.assertEqual(body["event_id"], "g02-exact-4096")

    def test_list_returns_only_latest_50_events(self):
        for number in range(51):
            event = self.fixture("valid-event.json")
            event["event_id"] = f"g02-latest-{number:02d}"
            status, _ = self.request("POST", "/events", body=event, token="reporter-secret")
            self.assertEqual(status, 201)
        status, body = self.request("GET", "/events", token="operator-secret")
        event_ids = [event["event_id"] for event in body["events"]]
        self.assertEqual(status, 200)
        self.assertEqual(len(event_ids), 50)
        self.assertNotIn("g02-latest-00", event_ids)
        self.assertEqual(event_ids[0], "g02-latest-50")

    def test_page_uses_text_content_and_no_browser_storage(self):
        with urlopen(self.base_url + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("?token=", page)

    def test_missing_event_returns_not_found(self):
        status, body = self.request("GET", "/events/no-such-event", token="operator-secret")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not_found"})


if __name__ == "__main__":
    unittest.main()
