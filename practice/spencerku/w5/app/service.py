#!/usr/bin/env python3
"""W5 inspection service with PostgreSQL persistence and idempotent writes."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
from threading import Lock
from urllib.parse import urlsplit

TOKEN_NAMES = ("REPORTER_TOKEN", "OPERATOR_TOKEN")
DB_NAMES = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
EVENT_FIELDS = {"event_id", "device_id", "observed_at", "type", "note"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
EVENT_TYPES = {"status", "anomaly", "test"}
MAX_BODY = 4096


def load_env(env_file):
    values = {name: "" for name in TOKEN_NAMES + DB_NAMES}
    for name in values:
        value = os.environ.get(name, "").strip()
        if value:
            values[name] = value
    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name in values and value.strip() and not values[name]:
            values[name] = value.strip()
    return values


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def validate_event(event):
    if not isinstance(event, dict):
        return "event must be a JSON object", None
    extra = set(event) - EVENT_FIELDS
    if extra:
        return "unexpected field", sorted(extra)[0]
    for field, maximum in (("event_id", 64), ("device_id", 32)):
        value = event.get(field)
        if not isinstance(value, str) or not 1 <= len(value) <= maximum or not IDENTIFIER.fullmatch(value):
            return "must contain only letters, digits, '-' or '_'", field
    observed_at = event.get("observed_at")
    if not isinstance(observed_at, str):
        return "must be an ISO 8601 timestamp with timezone", "observed_at"
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        return "must be an ISO 8601 timestamp with timezone", "observed_at"
    if event.get("type") not in EVENT_TYPES:
        return "must be one of status, anomaly, test", "type"
    if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > 200):
        return "must be a string of at most 200 characters", "note"
    return None, None


class EventStore:
    """Use PostgreSQL when configured; retain an in-memory mode for local tests."""

    def __init__(self, env_file):
        self.config = load_env(env_file)
        self.db_configured = all(self.config[name] for name in DB_NAMES)
        self.memory = {}
        self.memory_order = []
        self.lock = Lock()
        self.schema_ready = False

    def _connect(self):
        import psycopg2

        return psycopg2.connect(
            host=self.config["DB_HOST"],
            dbname=self.config["DB_NAME"],
            user=self.config["DB_USER"],
            password=self.config["DB_PASSWORD"],
            sslmode="verify-full",
            sslrootcert="/etc/inspection/rds-ca.pem",
            connect_timeout=5,
        )

    def _ensure_schema(self, connection):
        if self.schema_ready:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    type TEXT NOT NULL,
                    note TEXT,
                    received_at TEXT NOT NULL
                )
                """
            )
        connection.commit()
        self.schema_ready = True

    @staticmethod
    def _stored(event):
        stored = dict(event)
        stored["received_at"] = utc_now()
        return stored

    @staticmethod
    def _same_content(existing, event):
        return all(existing.get(field) == event.get(field) for field in EVENT_FIELDS)

    def insert(self, event):
        if not self.db_configured:
            with self.lock:
                existing = self.memory.get(event["event_id"])
                if existing is not None:
                    return (200 if self._same_content(existing, event) else 409), existing
                stored = self._stored(event)
                self.memory[event["event_id"]] = stored
                self.memory_order.append(event["event_id"])
                return 201, stored

        connection = None
        try:
            connection = self._connect()
            self._ensure_schema(connection)
            stored = self._stored(event)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO events
                        (event_id, device_id, observed_at, type, note, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        stored["event_id"], stored["device_id"], stored["observed_at"],
                        stored["type"], stored.get("note"), stored["received_at"],
                    ),
                )
            connection.commit()
            return 201, stored
        except Exception as error:
            if connection is not None:
                connection.rollback()
            if getattr(error, "pgcode", None) != "23505":
                raise
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events WHERE event_id = %s
                    """,
                    (event["event_id"],),
                )
                row = cursor.fetchone()
            if row is None:
                raise
            existing = dict(zip(
                ("event_id", "device_id", "observed_at", "type", "note", "received_at"), row
            ))
            return (200 if self._same_content(existing, event) else 409), existing
        finally:
            if connection is not None:
                connection.close()

    def list_latest(self):
        if not self.db_configured:
            with self.lock:
                return [self.memory[event_id] for event_id in reversed(self.memory_order[-50:])]
        connection = None
        try:
            connection = self._connect()
            self._ensure_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events ORDER BY received_at DESC LIMIT 50
                    """
                )
                rows = cursor.fetchall()
            return [
                dict(zip(("event_id", "device_id", "observed_at", "type", "note", "received_at"), row))
                for row in rows
            ]
        finally:
            if connection is not None:
                connection.close()

    def get(self, event_id):
        if not self.db_configured:
            with self.lock:
                return self.memory.get(event_id)
        connection = None
        try:
            connection = self._connect()
            self._ensure_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT event_id, device_id, observed_at, type, note, received_at
                    FROM events WHERE event_id = %s
                    """,
                    (event_id,),
                )
                row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip(
                ("event_id", "device_id", "observed_at", "type", "note", "received_at"), row
            ))
        finally:
            if connection is not None:
                connection.close()


def page_html():
    return """<!doctype html>
<html lang="zh-Hant">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Inspection events</title></head>
<body>
<main>
<h1>Inspection events</h1>
<label for="token">Operator token</label>
<input id="token" type="password" autocomplete="off">
<button id="load" type="button">Load events</button>
<p id="message" role="status"></p>
<table><thead><tr><th>Event ID</th><th>Device</th><th>Observed</th><th>Type</th><th>Note</th><th>Received</th></tr></thead><tbody id="events"></tbody></table>
</main>
<script>
const tokenInput = document.getElementById("token");
const message = document.getElementById("message");
const eventsBody = document.getElementById("events");
function cell(row, value) { const item = document.createElement("td"); item.textContent = value == null ? "" : String(value); row.appendChild(item); }
async function loadEvents() {
  message.textContent = "Loading...";
  eventsBody.replaceChildren();
  const response = await fetch("/events", {headers: {Authorization: "Bearer " + tokenInput.value}});
  const body = await response.json();
  if (!response.ok) { message.textContent = body.error || "Request failed"; return; }
  body.events.forEach((event) => { const row = document.createElement("tr"); cell(row, event.event_id); cell(row, event.device_id); cell(row, event.observed_at); cell(row, event.type); cell(row, event.note); cell(row, event.received_at); eventsBody.appendChild(row); });
  message.textContent = "Loaded " + body.events.length + " event(s).";
  tokenInput.value = "";
}
document.getElementById("load").addEventListener("click", loadEvents);
</script>
</body>
</html>"""


def make_server(version_file, port=8080, env_file="/etc/inspection/app.env"):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    env = load_env(env_file)
    store = EventStore(env_file)
    started = utc_now()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, body):
            data = json_bytes(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status, error, field=None):
            body = {"error": error}
            if field is not None:
                body["field"] = field
            self.send_json(status, body)

        def role(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return None
            supplied = header[7:]
            if env["REPORTER_TOKEN"] and hmac.compare_digest(supplied, env["REPORTER_TOKEN"]):
                return "reporter"
            if env["OPERATOR_TOKEN"] and hmac.compare_digest(supplied, env["OPERATOR_TOKEN"]):
                return "operator"
            return None

        def require_role(self, expected):
            actual = self.role()
            if actual is None:
                self.send_error_json(401, "unauthorized")
                return False
            if actual != expected:
                self.send_error_json(403, "forbidden")
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, {
                    "status": "ok", "service": "inspection", "version": version,
                    "started_at": started, "auth_configured": all(env[name] for name in TOKEN_NAMES),
                    "db_configured": store.db_configured,
                })
                return
            if path == "/":
                data = page_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/events":
                if not self.require_role("operator"):
                    return
                try:
                    self.send_json(200, {"events": store.list_latest()})
                except Exception as error:
                    print(f"database read failed: {type(error).__name__} {getattr(error, 'pgcode', '')}")
                    self.send_error_json(503, "database unavailable")
                return
            prefix = "/events/"
            if path.startswith(prefix) and path[len(prefix):] and "/" not in path[len(prefix):]:
                if not self.require_role("operator"):
                    return
                try:
                    event = store.get(path[len(prefix):])
                except Exception as error:
                    print(f"database read failed: {type(error).__name__} {getattr(error, 'pgcode', '')}")
                    self.send_error_json(503, "database unavailable")
                    return
                if event is None:
                    self.send_error_json(404, "not_found")
                else:
                    self.send_json(200, event)
                return
            self.send_error_json(404, "not_found")

        def do_POST(self):
            if urlsplit(self.path).path != "/events":
                self.send_error_json(404, "not_found")
                return
            if not self.require_role("reporter"):
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self.send_error_json(400, "Content-Type must be application/json", "Content-Type")
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.send_error_json(400, "request body must be at most 4096 bytes", "body")
                return
            raw = self.rfile.read(length)
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error_json(400, "body must be valid JSON", "body")
                return
            error, field = validate_event(event)
            if error:
                self.send_error_json(400, error, field)
                return
            try:
                status, stored = store.insert(event)
            except Exception as error:
                print(f"database write failed: {type(error).__name__} {getattr(error, 'pgcode', '')}")
                self.send_error_json(503, "database unavailable")
                return
            if status == 409:
                self.send_error_json(409, "event_id exists with different content", "event_id")
                return
            self.send_json(status, stored)

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
