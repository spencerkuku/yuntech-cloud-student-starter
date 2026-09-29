"""Offline W4 event API contract tests using the supplied JSON fixtures."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

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
matrix_spec = importlib.util.spec_from_file_location(
    "w04_rejection_matrix", ROOT / "tests/w04_rejection_matrix.py")
matrix = importlib.util.module_from_spec(matrix_spec)
matrix_spec.loader.exec_module(matrix)


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

    def test_seven_row_rejection_matrix(self):
        reporter_token = "reporter-test-token"
        operator_token = "operator-test-token"
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "app.env"
            env_file.write_text(
                f"REPORTER_TOKEN={reporter_token}\nOPERATOR_TOKEN={operator_token}\n",
                encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = matrix.main([
                    "--base-url", self.base, "--env-file", str(env_file)])

        report = output.getvalue()
        self.assertEqual(exit_code, 0)
        lines = report.splitlines()
        self.assertEqual(lines[0], "Health version: " + "b" * 40)
        self.assertEqual(len(lines[1:]), 7)
        self.assertNotIn(reporter_token, report)
        self.assertNotIn(operator_token, report)

        statuses = [201, 401, 403, 400, 409, 403, 200]
        bodies = []
        for number, (line, status) in enumerate(zip(lines[1:], statuses), start=1):
            prefix = f"{number}. HTTP {status} body="
            self.assertTrue(line.startswith(prefix), line)
            bodies.append(json.loads(line[len(prefix):]))

        first_event_id = bodies[0]["event_id"]
        self.assertTrue(first_event_id.startswith("g02-m4-"))
        self.assertIn("received_at", bodies[0])
        self.assertEqual(bodies[3]["field"], "observed_at")
        self.assertTrue(any(
            event.get("event_id") == first_event_id for event in bodies[6]["events"]))

    def test_matrix_loads_tokens_from_file_without_logging_them(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "app.env"
            env_file.write_text(
                "REPORTER_TOKEN=reporter-fixture-secret\n"
                "OPERATOR_TOKEN=operator-fixture-secret\n",
                encoding="utf-8")
            tokens = matrix.load_tokens(env_file)
        self.assertEqual(tokens, ("reporter-fixture-secret", "operator-fixture-secret"))

    def test_matrix_report_redacts_tokens_from_health_and_json_body(self):
        tokens = ("reporter-fixture-secret", "operator-fixture-secret")
        result = {
            "health_version": tokens[0],
            "rows": [{
                "number": 1,
                "status": 200,
                "body": {tokens[1]: tokens[0]},
            }],
        }
        report = matrix.format_report(result, tokens)
        self.assertNotIn(tokens[0], report)
        self.assertNotIn(tokens[1], report)

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
        self.assertIn("event.received_at", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("sessionStorage", html)


if __name__ == "__main__":
    unittest.main()
