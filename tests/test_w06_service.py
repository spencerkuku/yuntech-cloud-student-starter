import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w06_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class W06ServiceContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.version = Path(self.tmp.name) / "version"
        self.version.write_text("a" * 40)
        self.server = service.make_server(self.version, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_database_timestamps_serialize_and_compare_as_utc(self):
        value = datetime(2026, 10, 13, 3, 40, tzinfo=timezone.utc)
        event = service.EventStore._row_to_event(
            ("event-1", "device-1", value, "status", None, value)
        )
        self.assertEqual(event["observed_at"], "2026-10-13T03:40:00Z")
        self.assertEqual(event["received_at"], "2026-10-13T03:40:00Z")
        self.assertTrue(service.EventStore._same_timestamp(value, "2026-10-13T03:40:00Z"))

    def test_post_and_get_events_require_tokens_and_filter_devices(self):
        env = {"REPORTER_TOKEN": "reporter-secret", "OPERATOR_TOKEN": "operator-secret"}
        service.load_env(env)

        payload = {
            "event_id": "team-001",
            "device_id": "g03-d01",
            "observed_at": "2026-10-13T10:00:00+08:00",
            "type": "status",
            "note": "ok",
        }

        req = urllib.request.Request(
            self.base + "/events",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 401)

        req.add_header("Authorization", "Bearer reporter-secret")
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 201)
            body = json.load(response)
            self.assertEqual(body["event_id"], "team-001")
            self.assertEqual(body["device_id"], "g03-d01")

        operator_req = urllib.request.Request(
            self.base + "/events?device_id=g03-d01",
            headers={"Authorization": "Bearer operator-secret"},
            method="GET",
        )
        with urllib.request.urlopen(operator_req) as response:
            self.assertEqual(response.status, 200)
            rows = json.load(response)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_id"], "team-001")

    def test_duplicate_event_id_same_content_is_idempotent(self):
        env = {"REPORTER_TOKEN": "reporter-secret", "OPERATOR_TOKEN": "operator-secret"}
        service.load_env(env)
        payload = {
            "event_id": "team-dup",
            "device_id": "g03-d02",
            "observed_at": "2026-10-13T10:00:00+08:00",
            "type": "status",
        }
        req = urllib.request.Request(
            self.base + "/events",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer reporter-secret"},
            method="POST",
        )
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 201)

        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 200)
            body = json.load(response)
            self.assertEqual(body["event_id"], "team-dup")


if __name__ == "__main__":
    unittest.main()
