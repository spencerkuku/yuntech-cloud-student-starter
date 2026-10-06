#!/usr/bin/env python3
"""Inspection service with access control and PostgreSQL-backed event storage."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse


ALLOWED_EVENT_FIELDS = {"event_id", "device_id", "observed_at", "type", "note"}
REQUIRED_EVENT_FIELDS = ("event_id", "device_id", "observed_at", "type")
MAX_EVENT_BYTES = 4096


def load_app_env():
    return dict(os.environ)


def db_is_configured(env):
    return all(env.get(name) for name in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"))


def _database_error_types():
    try:
        import psycopg2
    except ImportError:
        return (ImportError, OSError)
    return (psycopg2.Error, OSError)


def _connect_db(env):
    import psycopg2

    return psycopg2.connect(
        host=env["DB_HOST"],
        dbname=env["DB_NAME"],
        user=env["DB_USER"],
        password=env["DB_PASSWORD"],
        sslmode="verify-full",
        sslrootcert="/etc/inspection/rds-ca.pem",
        connect_timeout=5,
    )


def _ensure_schema(connection):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id VARCHAR(64) PRIMARY KEY,
                device_id VARCHAR(32) NOT NULL,
                observed_at TEXT NOT NULL,
                "type" VARCHAR(16) NOT NULL,
                note VARCHAR(200),
                received_at TIMESTAMPTZ NOT NULL
            )
            """
        )
    connection.commit()


def _event_from_row(row):
    if row is None:
        return None
    received_at = row[5]
    if isinstance(received_at, datetime):
        received_at = received_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "event_id": row[0],
        "device_id": row[1],
        "observed_at": row[2],
        "type": row[3],
        "received_at": received_at,
    }
    if row[4] is not None:
        event["note"] = row[4]
    return event


def _event_values(event):
    return (
        event["event_id"],
        event["device_id"],
        event["observed_at"],
        event["type"],
        event.get("note"),
        datetime.now(timezone.utc),
    )


def _same_event(existing, submitted):
    return all(existing.get(field) == submitted.get(field) for field in REQUIRED_EVENT_FIELDS) and (
        existing.get("note") == submitted.get("note")
    )


def _insert_event(connection, event):
    import psycopg2

    _ensure_schema(connection)
    with connection.cursor() as cursor:
        try:
            cursor.execute(
                """
                INSERT INTO events (event_id, device_id, observed_at, "type", note, received_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING event_id, device_id, observed_at, event_type, note, received_at
                """,
                _event_values(event),
            )
            created = _event_from_row(cursor.fetchone())
            connection.commit()
            return 201, created
        except psycopg2.IntegrityError as exc:
            if getattr(exc, "pgcode", None) != "23505":
                connection.rollback()
                raise
            connection.rollback()

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_id, device_id, observed_at, "type", note, received_at
            FROM events WHERE event_id = %s
            """,
            (event["event_id"],),
        )
        existing = _event_from_row(cursor.fetchone())
    if existing is None:
        raise RuntimeError("unique event disappeared")
    if _same_event(existing, event):
        return 200, existing
    return 409, None


def _list_events(connection, limit=50):
    _ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_id, device_id, observed_at, "type", note, received_at
            FROM events ORDER BY received_at DESC, event_id LIMIT %s
            """,
            (limit,),
        )
        return [_event_from_row(row) for row in cursor.fetchall()]


def _get_event(connection, event_id):
    _ensure_schema(connection)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_id, device_id, observed_at, "type", note, received_at
            FROM events WHERE event_id = %s
            """,
            (event_id,),
        )
        return _event_from_row(cursor.fetchone())


def _validate_event(event):
    if not isinstance(event, dict):
        raise ValueError("body")
    extra_fields = set(event) - ALLOWED_EVENT_FIELDS
    if extra_fields:
        raise ValueError(sorted(extra_fields)[0])
    missing = [field for field in REQUIRED_EVENT_FIELDS if field not in event]
    if missing:
        raise ValueError(missing[0])

    for field, max_length in (("event_id", 64), ("device_id", 32)):
        value = event[field]
        if not isinstance(value, str) or len(value) > max_length:
            raise ValueError(field)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError(field)

    observed_at = event["observed_at"]
    if not isinstance(observed_at, str):
        raise ValueError("observed_at")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at")

    if not isinstance(event["type"], str) or event["type"] not in {"status", "anomaly", "test"}:
        raise ValueError("type")
    if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > 200):
        raise ValueError("note")
    return event


def _authorized_role(header, role, env):
    reporter = env.get("REPORTER_TOKEN", "")
    operator = env.get("OPERATOR_TOKEN", "")
    if not reporter or not operator or hmac.compare_digest(reporter, operator):
        return False, 401
    token = header or ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    reporter_match = hmac.compare_digest(token, reporter)
    operator_match = hmac.compare_digest(token, operator)
    if not reporter_match and not operator_match:
        return False, 401
    if (role == "reporter" and not reporter_match) or (role == "operator" and not operator_match):
        return False, 403
    return True, 200


def make_server(version_file, port=8080):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def _send_json(self, status, payload):
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self, body):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status, error, field):
            self._send_json(status, {"error": error, "field": field})

        def _require_role(self, role, env):
            allowed, status = _authorized_role(self.headers.get("Authorization"), role, env)
            if allowed:
                return True
            self._error(status, "unauthorized" if status == 401 else "forbidden", "Authorization")
            return False

        def _read_event(self):
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type")
            length_header = self.headers.get("Content-Length")
            if length_header is None or not length_header.isdecimal():
                raise ValueError("body")
            length = int(length_header)
            if length > MAX_EVENT_BYTES:
                raise ValueError("body")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("body") from exc
            return _validate_event(payload)

        def _with_db(self, action):
            env = load_app_env()
            if not db_is_configured(env):
                return False, None
            connection = None
            try:
                connection = _connect_db(env)
                return True, action(connection)
            finally:
                if connection is not None:
                    connection.close()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/health":
                env = load_app_env()
                self._send_json(200, {
                    "status": "ok",
                    "service": "inspection",
                    "version": version,
                    "started_at": started,
                    "auth_configured": bool(
                        env.get("REPORTER_TOKEN")
                        and env.get("OPERATOR_TOKEN")
                        and not hmac.compare_digest(env["REPORTER_TOKEN"], env["OPERATOR_TOKEN"])
                    ),
                    "db_configured": db_is_configured(env),
                })
                return
            if path == "/":
                self._send_html("""<!doctype html>
<html><head><meta charset="utf-8"><title>Inspection</title></head>
<body><h1>Inspection events</h1>
<label>Operator token <input id="token" type="password"></label>
<button id="load">Load events</button><pre id="output"></pre>
<script>
const output = document.getElementById('output');
document.getElementById('load').addEventListener('click', async () => {
  output.textContent = 'Loading...';
  const token = document.getElementById('token').value;
  const response = await fetch('/events', {headers: {Authorization: 'Bearer ' + token}});
  output.textContent = await response.text();
});
</script></body></html>""")
                return
            if path == "/events":
                if not self._require_role("operator", load_app_env()):
                    return
                try:
                    configured, result = self._with_db(_list_events)
                    if not configured:
                        self._error(503, "database not configured", "database")
                    else:
                        self._send_json(200, {"events": result})
                except _database_error_types():
                    self._error(503, "database unavailable", "database")
                return
            if path.startswith("/events/"):
                if not self._require_role("operator", load_app_env()):
                    return
                event_id = path.removeprefix("/events/")
                if not event_id or "/" in event_id:
                    self._error(404, "not_found", "event_id")
                    return
                try:
                    configured, result = self._with_db(lambda connection: _get_event(connection, event_id))
                    if not configured:
                        self._error(503, "database not configured", "database")
                    elif result is None:
                        self._error(404, "not_found", "event_id")
                    else:
                        self._send_json(200, result)
                except _database_error_types():
                    self._error(503, "database unavailable", "database")
                return
            self._error(404, "not_found", "path")

        def do_POST(self):
            if urlparse(self.path).path != "/events":
                self._error(404, "not_found", "path")
                return
            if not self._require_role("reporter", load_app_env()):
                return
            try:
                event = self._read_event()
            except ValueError as exc:
                self._error(400, "invalid event", str(exc))
                return
            try:
                configured, result = self._with_db(lambda connection: _insert_event(connection, event))
                if not configured:
                    self._error(503, "database not configured", "database")
                elif result[0] == 409:
                    self._error(409, "event_id conflict", "event_id")
                else:
                    self._send_json(result[0], result[1])
            except _database_error_types():
                self._error(503, "database unavailable", "database")

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
