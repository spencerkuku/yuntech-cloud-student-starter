"""Offline W4 checks: no AWS calls, no network beyond 127.0.0.1, no classroom answers.

Every event body used here is read from tests/fixtures/*.json so that the three
student-authored fixtures are genuinely exercised.
"""
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
spec = importlib.util.spec_from_file_location("w04_service", ROOT / "app/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

REPORTER = "fixture-reporter-token"
OPERATOR = "fixture-operator-token"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@contextlib.contextmanager
def tempfile_version():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "version"
        path.write_text("b" * 40, encoding="utf-8")
        yield path


class ServiceTestCase(unittest.TestCase):
    """Runs the real service on 127.0.0.1 with fake tokens; no AWS and no public network."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"REPORTER_TOKEN": REPORTER,
                                                "OPERATOR_TOKEN": OPERATOR})
        self.env.start()
        self.addCleanup(self.env.stop)
        with tempfile_version() as version:
            self.server = service.make_server(version, port=0)
        self.addCleanup(self.server.server_close)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join, 2)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, token=None, body=None, content_type="application/json",
             raw_body=None, raw_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = {}
        if raw_headers:
            headers.update(raw_headers)
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        payload = raw_body
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if payload is not None and content_type is not None:
            headers["Content-Type"] = content_type
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        try:
            return response.status, json.loads(text)
        except json.JSONDecodeError:
            return response.status, text


class FixturesAreRealFiles(unittest.TestCase):
    def test_three_fixtures_exist_and_are_json(self):
        names = sorted(p.name for p in FIXTURES.glob("*.json"))
        self.assertEqual(len(names), 3, f"expected 3 fixtures, found {names}")
        for name in names:
            self.assertIsInstance(fixture(name), dict)


class HappyPath(ServiceTestCase):
    def test_valid_fixture_is_accepted(self):
        event = fixture("event_01_valid.json")
        status, body = self.call("POST", "/events", token=REPORTER, body=event)
        self.assertEqual(status, 201)
        self.assertEqual(body["event_id"], event["event_id"])
        self.assertEqual(body["note"], event["note"])
        self.assertTrue(body["received_at"].endswith("Z"))

    def test_duplicate_event_id_is_409(self):
        event = fixture("event_01_valid.json")
        self.assertEqual(self.call("POST", "/events", token=REPORTER, body=event)[0], 201)
        status, body = self.call("POST", "/events", token=REPORTER, body=event)
        self.assertEqual(status, 409)
        self.assertEqual(body["field"], "event_id")

    def test_operator_can_read_list_and_single_event(self):
        event = fixture("event_01_valid.json")
        self.call("POST", "/events", token=REPORTER, body=event)
        status, listed = self.call("GET", "/events", token=OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual([item["event_id"] for item in listed], [event["event_id"]])
        status, single = self.call("GET", "/events/" + event["event_id"], token=OPERATOR)
        self.assertEqual(status, 200)
        self.assertEqual(single["received_at"], listed[0]["received_at"])

    def test_health_reports_auth_configured(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["auth_configured"])
        self.assertEqual(body["version"], "b" * 40)
        self.assertEqual(body["status"], "ok")


class RejectionsFromFixtures(ServiceTestCase):
    def test_observed_at_without_timezone_is_400(self):
        event = fixture("event_02_bad_observed_at.json")
        self.assertNotIn("+", event["observed_at"].replace("2026-09-29T10:00:00", ""))
        status, body = self.call("POST", "/events", token=REPORTER, body=event)
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "observed_at")

    def test_null_note_is_400(self):
        event = fixture("event_03_bad_note_null.json")
        self.assertIn("note", event)
        self.assertIsNone(event["note"])
        status, body = self.call("POST", "/events", token=REPORTER, body=event)
        self.assertEqual(status, 400)
        self.assertEqual(body["field"], "note")

    def test_rejected_event_is_not_stored(self):
        for name in ("event_02_bad_observed_at.json", "event_03_bad_note_null.json"):
            self.call("POST", "/events", token=REPORTER, body=fixture(name))
        self.assertEqual(self.call("GET", "/events", token=OPERATOR)[1], [])


class OrderIsIdentityThenRoleThenContent(ServiceTestCase):
    def test_no_token_with_broken_body_is_401_not_400(self):
        broken = fixture("event_02_bad_observed_at.json")
        self.assertEqual(self.call("POST", "/events", body=broken)[0], 401)
        self.assertEqual(self.call("POST", "/events", token=None, body={})[0], 401)

    def test_unknown_or_malformed_token_is_401(self):
        self.assertEqual(self.call("GET", "/events", token="not-a-real-token")[0], 401)
        self.assertEqual(self.call("GET", "/events", token="")[0], 401)

    def test_operator_may_not_post(self):
        status, body = self.call("POST", "/events", token=OPERATOR,
                                 body=fixture("event_01_valid.json"))
        self.assertEqual(status, 403)
        self.assertEqual(set(body), {"error", "field"})

    def test_reporter_may_not_read(self):
        self.assertEqual(self.call("GET", "/events", token=REPORTER)[0], 403)
        self.assertEqual(self.call("GET", "/events/g02-kelvin-0001", token=REPORTER)[0], 403)

    def test_unknown_event_id_is_404_for_operator(self):
        self.assertEqual(self.call("GET", "/events/g02-kelvin-9999", token=OPERATOR)[0], 404)

    def test_content_type_and_size_are_enforced(self):
        event = fixture("event_01_valid.json")
        self.assertEqual(self.call("POST", "/events", token=REPORTER, body=event,
                                   content_type="text/plain")[0], 400)
        oversized = json.dumps({**event, "note": "x" * 5000}).encode("utf-8")
        self.assertEqual(self.call("POST", "/events", token=REPORTER,
                                   raw_body=oversized)[0], 400)
        self.assertEqual(self.call("POST", "/events", token=REPORTER,
                                   raw_body=b"{not json")[0], 400)

    def test_error_responses_never_echo_token_or_body(self):
        status, body = self.call("POST", "/events", body=fixture("event_01_valid.json"))
        self.assertEqual(status, 401)
        self.assertNotIn(REPORTER, json.dumps(body))
        self.assertNotIn(OPERATOR, json.dumps(body))
        self.assertEqual(set(body), {"error", "field"})
        _, rejected = self.call("POST", "/events", token=REPORTER,
                                body=fixture("event_02_bad_observed_at.json"))
        self.assertEqual(set(rejected), {"error", "field"})


class DisplayPageIsSafe(ServiceTestCase):
    def page(self):
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        return body

    def test_page_uses_text_content_and_keeps_token_out_of_the_browser(self):
        page = self.page()
        self.assertIn("textContent", page)
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                          "localStorage", "sessionStorage", "?token=", "eval("):
            self.assertNotIn(forbidden, page)
        self.assertIn('Authorization: "Bearer " + operatorToken', page)
        self.assertIn('tokenInput.value = ""', page)

    def test_page_needs_no_token_to_load(self):
        self.assertIn("<table", self.page())


if __name__ == "__main__":
    unittest.main()
