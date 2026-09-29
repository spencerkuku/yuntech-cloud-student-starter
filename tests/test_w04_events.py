"""Offline W4 event API contract tests using the supplied JSON fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures"
FIXTURES = {
    name: json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
    for name, filename in (
        ("valid", "event_valid.json"),
        ("bad_time", "event_bad_time.json"),
        ("bad_type", "event_bad_type.json"),
    )
}
spec = importlib.util.spec_from_file_location("w04_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class EventApiContract(unittest.TestCase):
    def setUp(self):
        self.original_tokens = {
            key: os.environ.get(key)
            for key in ("REPORTER_TOKEN", "OPERATOR_TOKEN")
        }
        os.environ["REPORTER_TOKEN"] = "reporter-test-token"
        os.environ["OPERATOR_TOKEN"] = "operator-test-token"
        self.temp_dir = tempfile.TemporaryDirectory()
        version_file = Path(self.temp_dir.name) / "version"
        version_file.write_text("b" * 40, encoding="utf-8")
        self.server = service.make_server(version_file, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.temp_dir.cleanup()
        for key, value in self.original_tokens.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def request(self, method, path, body=None, token=None, content_type="application/json"):
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
        elif body is None:
            data = None
        else:
            data = json.dumps(body).encode("utf-8")
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if data is not None and content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = response.read()
                return response.status, payload
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def post_fixture(self, fixture, token="reporter-test-token"):
        return self.request("POST", "/events", body=fixture, token=token)

    def json_request(self, method, path, **kwargs):
        status, payload = self.request(method, path, **kwargs)
        return status, json.loads(payload.decode("utf-8"))

    def test_auth_authorization_validation_duplicate_and_created_order(self):
        seed = dict(FIXTURES["valid"], event_id="duplicate-seed")
        self.assertEqual(self.post_fixture(seed)[0], 201)

        status, error = self.json_request("POST", "/events", body="{", token=None)
        self.assertEqual((status, error["error"]), (401, "authentication_required"))

        status, error = self.json_request(
            "POST", "/events", body="{", token="operator-test-token")
        self.assertEqual((status, error["error"]), (403, "forbidden"))

        status, error = self.json_request(
            "POST", "/events", body=FIXTURES["bad_type"], token="reporter-test-token")
        self.assertEqual((status, error["error"]), (400, "invalid_event"))
        self.assertEqual(error["field"], "type")

        status, error = self.json_request(
            "POST", "/events", body=seed, token="reporter-test-token")
        self.assertEqual((status, error["error"]), (409, "duplicate_event"))

        status, created = self.json_request(
            "POST", "/events", body=FIXTURES["valid"], token="reporter-test-token")
        self.assertEqual(status, 201)
        self.assertEqual(created["event_id"], FIXTURES["valid"]["event_id"])
        self.assertRegex(created["received_at"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertEqual(set(created), set(FIXTURES["valid"]) | {"received_at"})

    def test_all_fixtures_are_used_and_bad_time_is_rejected(self):
        status, error = self.json_request(
            "POST", "/events", body=FIXTURES["bad_time"], token="reporter-test-token")
        self.assertEqual((status, error["error"], error["field"]),
                         (400, "invalid_event", "observed_at"))
        status, error = self.json_request(
            "POST", "/events", body=FIXTURES["bad_type"], token="reporter-test-token")
        self.assertEqual((status, error["error"], error["field"]), (400, "invalid_event", "type"))
        self.assertEqual(self.post_fixture(FIXTURES["valid"])[0], 201)

    def test_validation_rejects_schema_headers_size_and_json_errors(self):
        valid = FIXTURES["valid"]
        cases = [
            (dict(valid, unexpected=True), "application/json", "unexpected"),
            (dict(valid, note=None), "application/json", "note"),
            (dict(valid, note="x" * 201), "application/json", "note"),
            (dict(valid, type=[]), "application/json", "type"),
            (dict(valid, event_id="invalid.id"), "application/json", "event_id"),
            (dict(valid, device_id="invalid.id"), "application/json", "device_id"),
            ({key: value for key, value in valid.items() if key != "event_id"},
             "application/json", "event_id"),
        ]
        for body, content_type, field in cases:
            with self.subTest(field=field, body=body):
                status, error = self.json_request(
                    "POST", "/events", body=body, token="reporter-test-token",
                    content_type=content_type)
                self.assertEqual(status, 400)
                self.assertEqual(error["field"], field)

        status, error = self.json_request(
            "POST", "/events", body=valid, token="reporter-test-token",
            content_type="text/plain")
        self.assertEqual((status, error["field"]), (400, "content_type"))

        status, error = self.json_request(
            "POST", "/events", body=b"{" + b" " * 4096,
            token="reporter-test-token")
        self.assertEqual((status, error["field"]), (400, "body"))

        status, error = self.json_request(
            "POST", "/events", body="{", token="reporter-test-token")
        self.assertEqual((status, error["field"]), (400, "body"))

    def test_operator_can_list_latest_50_and_get_single(self):
        base_event = FIXTURES["valid"]
        for index in range(51):
            event = dict(base_event, event_id=f"batch-{index:02d}")
            self.assertEqual(self.post_fixture(event)[0], 201)

        status, result = self.json_request(
            "GET", "/events", token="operator-test-token")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["events"]), 50)
        self.assertEqual(result["events"][0]["event_id"], "batch-50")
        self.assertEqual(result["events"][-1]["event_id"], "batch-01")

        status, event = self.json_request(
            "GET", "/events/batch-25", token="operator-test-token")
        self.assertEqual((status, event["event_id"]), (200, "batch-25"))

    def test_role_restrictions_and_missing_event(self):
        status, error = self.json_request("GET", "/events", token="reporter-test-token")
        self.assertEqual((status, error["error"]), (403, "forbidden"))
        status, error = self.json_request(
            "GET", "/events/not-present", token="operator-test-token")
        self.assertEqual((status, error["error"], error["field"]),
                         (404, "not_found", "event_id"))
        status, error = self.json_request("GET", "/events", token="wrong-token")
        self.assertEqual((status, error["error"]), (401, "invalid_token"))

    def test_identical_role_tokens_do_not_grant_both_roles(self):
        os.environ["OPERATOR_TOKEN"] = "reporter-test-token"
        status, error = self.json_request(
            "GET", "/events", token="reporter-test-token")
        self.assertEqual((status, error["error"]), (403, "forbidden"))
        status, error = self.json_request(
            "POST", "/events", body=FIXTURES["valid"], token="reporter-test-token")
        self.assertEqual((status, error["error"]), (403, "forbidden"))

    def test_health_auth_configuration_and_manual_token_page(self):
        status, health = self.json_request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["auth_configured"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["service"], "inspection")
        self.assertEqual(health["version"], "b" * 40)
        self.assertTrue(health["started_at"].endswith("Z"))

        os.environ.pop("OPERATOR_TOKEN")
        status, health = self.json_request("GET", "/health")
        self.assertEqual((status, health["auth_configured"]), (200, False))
        os.environ["OPERATOR_TOKEN"] = "operator-test-token"

        status, page = self.request("GET", "/")
        html = page.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('type="password"', html)
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("sessionStorage", html)


if __name__ == "__main__":
    unittest.main()
