#!/usr/bin/env python3
"""W4 inspection service: the W3 /health probe plus event intake, query and a display page.

Tokens arrive in the process environment, which systemd fills from the root-only
file /etc/inspection/app.env (packaged by deploy/make_user_data.py). This process
never opens that file, never writes a token or an event body to a log, and never
echoes either back to a client.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import urllib.parse

MAX_BODY = 4096          # 4 KiB
MAX_LIST = 50
MAX_NOTE = 200
EVENT_TYPES = ("status", "anomaly", "test")
EVENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
# ISO 8601 that must carry an offset or Z; a naive local time is rejected.
OBSERVED_AT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")

DISPLAY_PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>巡檢事件</title>
<style>
body{font-family:system-ui,sans-serif;margin:1.5rem;color:#1b1b1b}
table{border-collapse:collapse;margin-top:1rem;width:100%}
th,td{border:1px solid #bbb;padding:.35rem .5rem;text-align:left;font-size:.9rem}
th{background:#f2f2f2}
input{padding:.3rem;min-width:22rem}
#status{font-weight:600}
</style>
</head>
<body>
<h1>巡檢事件</h1>
<p>貼上 operator 權杖後按「讀取」。權杖只留在此頁面的記憶體，不寫入網址或瀏覽器儲存，送出後即清空。</p>
<form id="load">
<label for="token">operator 權杖</label>
<input id="token" type="password" autocomplete="off" spellcheck="false" required>
<button type="submit">讀取</button>
</form>
<p id="status"></p>
<table>
<thead><tr><th>event_id</th><th>device_id</th><th>observed_at</th><th>type</th><th>note</th><th>received_at</th></tr></thead>
<tbody id="events"></tbody>
</table>
<script>
const form = document.getElementById("load");
const tokenInput = document.getElementById("token");
const statusLine = document.getElementById("status");
const tableBody = document.getElementById("events");
let operatorToken = "";

form.addEventListener("submit", async function (event) {
  event.preventDefault();
  operatorToken = tokenInput.value;
  tokenInput.value = "";
  statusLine.textContent = "讀取中…";
  tableBody.replaceChildren();
  let response, data;
  try {
    response = await fetch("/events", { headers: { Authorization: "Bearer " + operatorToken } });
    data = await response.json();
  } catch (err) {
    operatorToken = "";
    statusLine.textContent = "無法讀取：" + err.message;
    return;
  }
  operatorToken = "";
  if (!response.ok) {
    statusLine.textContent = "HTTP " + response.status + "　" + (data.error || "") +
      (data.field ? "（" + data.field + "）" : "");
    return;
  }
  statusLine.textContent = "共 " + data.length + " 筆（最多顯示最新 50 筆）";
  for (const item of data) {
    const row = document.createElement("tr");
    for (const key of ["event_id", "device_id", "observed_at", "type", "note", "received_at"]) {
      const cell = document.createElement("td");
      cell.textContent = item[key] === undefined ? "" : String(item[key]);
      row.appendChild(cell);
    }
    tableBody.appendChild(row);
  }
});
</script>
</body>
</html>
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_event(payload):
    """Return (event, None) or (None, (error, field)) for a decoded request body."""
    if not isinstance(payload, dict):
        return None, ("invalid_body", "body")
    for field in sorted(payload):
        if field not in ("event_id", "device_id", "observed_at", "type", "note"):
            return None, ("unexpected_field", field)
    for field in ("event_id", "device_id", "observed_at", "type"):
        if field not in payload:
            return None, ("missing_field", field)
    if not isinstance(payload["event_id"], str) or not EVENT_ID_RE.fullmatch(payload["event_id"]):
        return None, ("invalid_value", "event_id")
    if not isinstance(payload["device_id"], str) or not DEVICE_ID_RE.fullmatch(payload["device_id"]):
        return None, ("invalid_value", "device_id")
    if not isinstance(payload["observed_at"], str) or not OBSERVED_AT_RE.fullmatch(payload["observed_at"]):
        return None, ("invalid_value", "observed_at")
    if payload["type"] not in EVENT_TYPES:
        return None, ("invalid_value", "type")
    event = {field: payload[field] for field in ("event_id", "device_id", "observed_at", "type")}
    if "note" in payload:
        note = payload["note"]
        if not isinstance(note, str):
            return None, ("invalid_value", "note")
        if len(note) > MAX_NOTE:
            return None, ("note_too_long", "note")
        event["note"] = note
    return event, None


class EventStore:
    """In-memory only by design; W5 replaces this with the shared database."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = {}

    def add(self, event):
        with self._lock:
            if event["event_id"] in self._events:
                return False
            self._events[event["event_id"]] = event
            return True

    def get(self, event_id):
        with self._lock:
            return self._events.get(event_id)

    def latest(self, limit=MAX_LIST):
        with self._lock:
            items = list(self._events.values())
        return list(reversed(items[-limit:]))


def make_server(version_file, port=8080):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = utc_now()
    store = EventStore()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        # --- response helpers; bodies only ever carry error/field or event data ---
        def _send(self, code, payload, content_type="application/json; charset=utf-8"):
            data = (payload if isinstance(payload, str)
                    else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code, error, field=None):
            body = {"error": error}
            if field is not None:
                body["field"] = field
            return self._send(code, body)

        def _role(self, token):
            reporter = os.environ.get("REPORTER_TOKEN", "")
            operator = os.environ.get("OPERATOR_TOKEN", "")
            if reporter and secrets.compare_digest(token, reporter):
                return "reporter"
            if operator and secrets.compare_digest(token, operator):
                return "operator"
            return None

        def _identity(self):
            """Return 'reporter', 'operator' or None. The token is never stored or logged."""
            header = self.headers.get("Authorization")
            if not header:
                return None
            scheme, _, token = header.partition(" ")
            token = token.strip()
            if scheme.lower() != "bearer" or not token:
                return None
            return self._role(token)

        def _auth_configured(self):
            reporter = os.environ.get("REPORTER_TOKEN", "")
            operator = os.environ.get("OPERATOR_TOKEN", "")
            return bool(reporter) and bool(operator) and not secrets.compare_digest(reporter, operator)

        def _route(self):
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def do_GET(self):
            route = self._route()
            if route == "/":
                return self._send(200, DISPLAY_PAGE, "text/html; charset=utf-8")
            if route == "/health":
                return self._send(200, {"status": "ok", "service": "inspection",
                                        "version": version, "started_at": started,
                                        "auth_configured": self._auth_configured()})
            if route == "/events":
                who = self._identity()
                if who is None:
                    return self._error(401, "unauthorized", "authorization")
                if who != "operator":
                    return self._error(403, "forbidden", "role")
                return self._send(200, store.latest())
            if route.startswith("/events/"):
                who = self._identity()
                if who is None:
                    return self._error(401, "unauthorized", "authorization")
                if who != "operator":
                    return self._error(403, "forbidden", "role")
                event = store.get(urllib.parse.unquote(route[len("/events/"):]))
                if event is None:
                    return self._error(404, "not_found", "event_id")
                return self._send(200, event)
            return self._error(404, "not_found", "path")

        def do_POST(self):
            if self._route() != "/events":
                return self._error(404, "not_found", "path")
            # 1. who are you  2. may you do this  -- both before the body is examined
            who = self._identity()
            if who is None:
                return self._error(401, "unauthorized", "authorization")
            if who != "reporter":
                return self._error(403, "forbidden", "role")
            # 3. only a known reporter learns anything about the event contract
            media = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if media != "application/json":
                return self._error(400, "unsupported_media_type", "content_type")
            if self.headers.get("Transfer-Encoding"):
                return self._error(400, "invalid_body", "body")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._error(400, "invalid_body", "body")
            if length <= 0:
                return self._error(400, "empty_body", "body")
            if length > MAX_BODY:
                return self._error(400, "body_too_large", "body")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._error(400, "invalid_json", "body")
            event, problem = validate_event(payload)
            if problem is not None:
                return self._error(400, problem[0], problem[1])
            event["received_at"] = utc_now()
            if not store.add(event):
                return self._error(409, "duplicate_event_id", "event_id")
            return self._send(201, event)

        def do_DELETE(self):
            return self._error(405, "method_not_allowed", "method")

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    return server


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
