"""W4 offline event API contract tests; no AWS calls or real secrets."""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

spec = importlib.util.spec_from_file_location(
    "w04_service", ROOT / "app/service.py"
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

REPORTER_TOKEN = "offline-test-reporter"
OPERATOR_TOKEN = "offline-test-operator"


def load_fixture(name):
    with (FIXTURES / name).open(encoding="utf-8") as f:
        return json.load(f)


class W04ServiceContract(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "REPORTER_TOKEN": REPORTER_TOKEN,
                "OPERATOR_TOKEN": OPERATOR_TOKEN,
            },
        )
        self.env.start()

        self.tempdir = tempfile.TemporaryDirectory()
        version = Path(self.tempdir.name) / "version"
        version.write_text("a" * 40, encoding="utf-8")

        self.server = service.make_server(version, port=0)
        self.worker = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.tempdir.cleanup()
        self.env.stop()

    def request(
        self,
        method,
        path,
        *,
        token=None,
        body=None,
        content_type="application/json",
    ):
        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=2,
        )

        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type

        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        conn.close()

        return status, json.loads(raw.decode("utf-8"))

    def test_health_reports_auth_configured(self):
        status, body = self.request("GET", "/health")

        self.assertEqual(status, 200)
        self.assertTrue(body["auth_configured"])
        self.assertEqual(body["version"], "a" * 40)

    def test_valid_fixture_is_created(self):
        event = load_fixture("event_valid.json")

        status, body = self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )

        self.assertEqual(status, 201)
        self.assertEqual(body["event_id"], event["event_id"])
        self.assertIn("received_at", body)

    def test_invalid_time_fixture_is_rejected(self):
        event = load_fixture("event_invalid_time.json")

        status, body = self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )

        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "observed_at")

    def test_extra_field_fixture_is_rejected(self):
        event = load_fixture("event_invalid_extra_field.json")

        status, body = self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )

        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "extra")

    def test_401_happens_before_validation(self):
        event = load_fixture("event_invalid_time.json")

        status, _ = self.request(
            "POST",
            "/events",
            body=event,
        )

        self.assertEqual(status, 401)

    def test_operator_cannot_post(self):
        event = load_fixture("event_valid.json")

        status, _ = self.request(
            "POST",
            "/events",
            token=OPERATOR_TOKEN,
            body=event,
        )

        self.assertEqual(status, 403)

    def test_duplicate_returns_409(self):
        event = load_fixture("event_valid.json")

        first, _ = self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )
        second, _ = self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )

        self.assertEqual(first, 201)
        self.assertEqual(second, 409)

    def test_reporter_cannot_read_list(self):
        status, _ = self.request(
            "GET",
            "/events",
            token=REPORTER_TOKEN,
        )

        self.assertEqual(status, 403)

    def test_operator_can_read_list(self):
        event = load_fixture("event_valid.json")

        created, _ = self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )
        self.assertEqual(created, 201)

        status, body = self.request(
            "GET",
            "/events",
            token=OPERATOR_TOKEN,
        )

        self.assertEqual(status, 200)
        self.assertTrue(
            any(
                item["event_id"] == event["event_id"]
                for item in body["events"]
            )
        )

    def test_operator_can_read_one_event(self):
        event = load_fixture("event_valid.json")

        self.request(
            "POST",
            "/events",
            token=REPORTER_TOKEN,
            body=event,
        )

        status, body = self.request(
            "GET",
            f"/events/{event['event_id']}",
            token=OPERATOR_TOKEN,
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["event_id"], event["event_id"])

    def test_missing_event_returns_404(self):
        status, body = self.request(
            "GET",
            "/events/does-not-exist",
            token=OPERATOR_TOKEN,
        )

        self.assertEqual(status, 404)
        self.assertEqual(body["field"], "event_id")

    def test_display_page_uses_safe_rendering(self):
        conn = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=2,
        )
        conn.request("GET", "/")
        response = conn.getresponse()
        page = response.read().decode("utf-8")
        status = response.status
        conn.close()

        self.assertEqual(status, 200)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("?token=", page)


if __name__ == "__main__":
    unittest.main()
