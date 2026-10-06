"""Offline W5 contract tests; database behavior is represented by an in-memory adapter."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w05_service", ROOT / "app" / "service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
matrix_spec = importlib.util.spec_from_file_location(
    "w05_matrix", ROOT / "tests" / "w05_idempotency_matrix.py",
)
matrix = importlib.util.module_from_spec(matrix_spec)
matrix_spec.loader.exec_module(matrix)


class FakePsycopgError(Exception):
    pass


class UniqueViolation(FakePsycopgError):
    pgcode = "23505"


class MemoryDatabase:
    def __init__(self):
        self.rows = {}
        self.sql_calls = []

    def connect(self, env=None, **kwargs):
        return MemoryConnection(self)


class MemoryConnection:
    def __init__(self, database):
        self.database = database
        self.pending_row = None
        self.closed = False

    def cursor(self):
        return MemoryCursor(self)

    def commit(self):
        if self.pending_row is not None:
            self.database.rows[self.pending_row[0]] = self.pending_row
            self.pending_row = None

    def rollback(self):
        self.pending_row = None

    def close(self):
        self.closed = True


class MemoryCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.results = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, statement, parameters=None):
        sql = " ".join(statement.split())
        self.connection.database.sql_calls.append((sql, parameters))
        if sql.startswith("CREATE TABLE"):
            return
        if sql.startswith("INSERT INTO events"):
            event_id = parameters[0]
            if event_id in self.connection.database.rows:
                raise UniqueViolation()
            self.connection.pending_row = tuple(parameters)
            self.result = self.connection.pending_row
            return
        if sql.startswith("SELECT") and "WHERE event_id = %s" in sql:
            self.result = self.connection.database.rows.get(parameters[0])
            return
        if sql.startswith("SELECT") and "ORDER BY received_at" in sql:
            rows = list(self.connection.database.rows.values())
            self.results = list(reversed(rows))[:parameters[0]]
            return
        raise AssertionError(f"Unexpected SQL shape: {sql}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.results


class W05ServiceContract(unittest.TestCase):
    def setUp(self):
        self.tempdir = __import__("tempfile").TemporaryDirectory()
        self.version = Path(self.tempdir.name) / "version"
        self.version.write_text("a" * 40, encoding="utf-8")
        self.database = MemoryDatabase()
        self.patcher_env = patch.object(service, "load_app_env", return_value={
            "REPORTER_TOKEN": "reporter-test-token",
            "OPERATOR_TOKEN": "operator-test-token",
            "DB_HOST": "db.invalid",
            "DB_NAME": "inspection",
            "DB_USER": "inspection_app",
            "DB_PASSWORD": "not-printed",
        })
        self.patcher_connect = patch.object(service, "_connect_db", side_effect=self.database.connect)
        self.patcher_psycopg2 = patch.dict(sys.modules, {
            "psycopg2": types.SimpleNamespace(IntegrityError=UniqueViolation, Error=FakePsycopgError),
        })
        self.patcher_env.start()
        self.patcher_connect.start()
        self.patcher_psycopg2.start()
        self.server = service.make_server(self.version, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.event = {
            "event_id": "g03-m2-0001",
            "device_id": "g03-d01",
            "observed_at": "2026-10-06T01:00:00+00:00",
            "type": "status",
            "note": "normal",
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.patcher_psycopg2.stop()
        self.patcher_connect.stop()
        self.patcher_env.stop()
        self.tempdir.cleanup()

    def request(self, path, method="GET", token=None, body=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_health_reports_db_configuration_and_keeps_health_available(self):
        status, result = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(result["version"], "a" * 40)
        self.assertIs(result["db_configured"], True)
        with patch.object(service, "load_app_env", return_value={
            "REPORTER_TOKEN": "same",
            "OPERATOR_TOKEN": "same",
            "DB_HOST": "db.invalid",
            "DB_NAME": "inspection",
            "DB_USER": "inspection_app",
            "DB_PASSWORD": "not-printed",
        }):
            status, result = self.request("/health")
        self.assertEqual(status, 200)
        self.assertIs(result["auth_configured"], False)
        with patch.object(service, "load_app_env", return_value={}):
            status, result = self.request("/health")
        self.assertEqual(status, 200)
        self.assertIs(result["db_configured"], False)

    def test_authentication_precedes_validation_and_role_checks(self):
        invalid = {"unknown": "secret event content"}
        self.assertEqual(self.request("/events", "POST", body=invalid)[0], 401)
        self.assertEqual(self.request("/events", "POST", "operator-test-token", invalid)[0], 403)
        self.assertEqual(self.request("/events", "POST", "reporter-test-token", invalid)[0], 400)
        self.assertEqual(self.request("/events", token="reporter-test-token")[0], 403)

    def test_insert_duplicate_and_conflict_follow_primary_key_result(self):
        self.event["note"] = "normal'); DROP TABLE events;--"
        status1, created = self.request("/events", "POST", "reporter-test-token", self.event)
        status2, duplicate = self.request("/events", "POST", "reporter-test-token", self.event)
        changed = dict(self.event, note="different")
        status3, conflict = self.request("/events", "POST", "reporter-test-token", changed)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertEqual(status3, 409)
        self.assertEqual(created["received_at"], duplicate["received_at"])
        self.assertNotIn("not-printed", json.dumps(conflict))

        writes = [(sql, params) for sql, params in self.database.sql_calls if sql.startswith("INSERT")]
        self.assertEqual(len(writes), 3)
        for sql, parameters in writes:
            self.assertIn("%s", sql)
            self.assertNotIn(self.event["event_id"], sql)
            self.assertNotIn(self.event["note"], sql)
            self.assertIsInstance(parameters, tuple)
        duplicate_reads = [
            sql for sql, _ in self.database.sql_calls
            if sql.startswith("SELECT") and "WHERE event_id = %s" in sql
        ]
        self.assertEqual(len(duplicate_reads), 2)
        sql_statements = [sql for sql, _ in self.database.sql_calls]
        for sql in duplicate_reads:
            index = sql_statements.index(sql)
            self.assertTrue(any(item.startswith("INSERT INTO events") for item in sql_statements[:index]))

    def test_list_get_and_invalid_event_contract(self):
        self.request("/events", "POST", "reporter-test-token", self.event)
        status, rows = self.request("/events", token="operator-test-token")
        self.assertEqual(status, 200)
        self.assertEqual(rows["events"][0]["event_id"], self.event["event_id"])
        status, row = self.request("/events/" + self.event["event_id"], token="operator-test-token")
        self.assertEqual(status, 200)
        self.assertEqual(row["event_id"], self.event["event_id"])

        no_timezone = dict(self.event, event_id="g03-m2-0002", observed_at="2026-10-06T01:00:00")
        self.assertEqual(self.request("/events", "POST", "reporter-test-token", no_timezone)[0], 400)
        null_note = dict(self.event, event_id="g03-m2-0003", note=None)
        self.assertEqual(self.request("/events", "POST", "reporter-test-token", null_note)[0], 400)
        oversized = dict(self.event, event_id="g03-m2-0004", note="x" * 201)
        self.assertEqual(self.request("/events", "POST", "reporter-test-token", oversized)[0], 400)

    def test_event_survives_service_recreation_against_the_same_database(self):
        status, created = self.request("/events", "POST", "reporter-test-token", self.event)
        self.assertEqual(status, 201)
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.server = service.make_server(self.version, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        status, result = self.request(
            "/events/" + self.event["event_id"], token="operator-test-token",
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["event_id"], self.event["event_id"])
        self.assertEqual(result["received_at"], created["received_at"])

    def test_database_unconfigured_returns_health_and_explicit_api_failure(self):
        with patch.object(service, "load_app_env", return_value={"REPORTER_TOKEN": "reporter", "OPERATOR_TOKEN": "operator"}):
            self.assertEqual(self.request("/health")[0], 200)
            status, body = self.request("/events", "POST", "reporter", self.event)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "database not configured")

    def test_sql_source_uses_bound_values_and_insert_first(self):
        source = (ROOT / "app" / "service.py").read_text(encoding="utf-8")
        insert_at = source.index("INSERT INTO events")
        duplicate_select_at = source.index("SELECT event_id", insert_at)
        self.assertLess(insert_at, duplicate_select_at)
        self.assertIn("VALUES (%s, %s, %s, %s, %s, %s)", source)
        self.assertIn("WHERE event_id = %s", source)

    def test_matrix_redacts_secrets_and_connection_strings_in_response_bodies(self):
        body = {
            "note": (
                "reporter-test-token postgres://user:password@db.invalid/inspection "
                "host=db.invalid dbname=inspection user=inspection_app "
                "password=not-printed"
            ),
            "password": "not-printed",
        }
        redacted = json.dumps(matrix.redact_body(
            body,
            ("reporter-test-token", "operator-test-token", "not-printed"),
        ))
        for sensitive in ("reporter-test-token", "operator-test-token", "not-printed", "db.invalid"):
            self.assertNotIn(sensitive, redacted)


if __name__ == "__main__":
    unittest.main()
