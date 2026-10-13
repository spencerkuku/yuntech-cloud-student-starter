#!/usr/bin/env python3
"""W4 inspection service: the W3 /health probe plus event intake, query and a display page.

Tokens arrive in the process environment, which systemd fills from the root-only
file /etc/inspection/app.env (packaged by deploy/make_user_data.py). This process
never opens that file, never writes a token or an event body to a log, and never
echoes either back to a client. Events use PostgreSQL when configured and an
in-memory store for offline W3/W4 testing.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import urllib.parse

try:
    import psycopg2
except Exception:
    psycopg2 = None

MAX_BODY_BYTES = 4096
EVENT_FIELDS = {"event_id", "device_id", "observed_at", "type", "note"}
REQUIRED_EVENT_FIELDS = {"event_id", "device_id", "observed_at", "type"}
EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
MAX_LIST = 50
MAX_NOTE = 200
EVENT_TYPES = ("status", "anomaly", "test")

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
    statusLine.textContent = "共 " + data.events.length + " 筆（最多顯示最新 50 筆）";
    for (const item of data.events) {
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


class EventStore:
    """Use PostgreSQL when configured, otherwise keep an offline in-memory store."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mem = {}
        self._db_ok = False
        self._conn = None
        self._init_db()

    def _env_db_ok(self):
        host = os.environ.get("DB_HOST", "")
        name = os.environ.get("DB_NAME", "")
        user = os.environ.get("DB_USER", "")
        pwd = os.environ.get("DB_PASSWORD", "")
        return bool(host and name and user and pwd and psycopg2 is not None)

    def _init_db(self):
        if not self._env_db_ok():
            self._db_ok = False
            return
        try:
            conn = psycopg2.connect(
                host=os.environ["DB_HOST"],
                dbname=os.environ["DB_NAME"],
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"],
                sslmode="verify-full",
                sslrootcert="/etc/inspection/rds-ca.pem",
                connect_timeout=5,
            )
            conn.autocommit = False
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    type TEXT NOT NULL,
                    note TEXT,
                    received_at TEXT NOT NULL
                )
            """)
            conn.commit()
            cur.close()
            self._conn = conn
            self._db_ok = True
        except Exception:
            self._db_ok = False
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None

    def db_configured(self):
        return self._db_ok

    def add(self, event):
        if self._db_ok and self._conn:
            try:
                cur = self._conn.cursor()
                try:
                    cur.execute(
                        "INSERT INTO events (event_id, device_id, observed_at, type, note, received_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (
                            event["event_id"],
                            event["device_id"],
                            event["observed_at"],
                            event["type"],
                            event.get("note"),
                            event["received_at"],
                        ),
                    )
                    self._conn.commit()
                    cur.close()
                    return True
                except psycopg2.IntegrityError:
                    self._conn.rollback()
                    cur.close()
                    # check if same content
                    cur = self._conn.cursor()
                    cur.execute(
                        "SELECT device_id, observed_at, type, note FROM events WHERE event_id=%s",
                        (event["event_id"],),
                    )
                    row = cur.fetchone()
                    cur.close()
                    if row is None:
                        return False
                    same = (
                        row[0] == event["device_id"]
                        and row[1] == event["observed_at"]
                        and row[2] == event["type"]
                        and (row[3] if row[3] is not None else "") == (event.get("note") if event.get("note") is not None else "")
                    )
                    if same:
                        return "same"
                    return "conflict"
                except Exception:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                    try:
                        cur.close()
                    except Exception:
                        pass
                    # fall through to mem? but prefer DB semantics; return False on error? but spec states handle
                    return False
            except Exception:
                return False
        with self._lock:
            if event["event_id"] in self._mem:
                old = self._mem[event["event_id"]]
                same = (
                    old["device_id"] == event["device_id"]
                    and old["observed_at"] == event["observed_at"]
                    and old["type"] == event["type"]
                    and (old.get("note") if old.get("note") is not None else "") == (event.get("note") if event.get("note") is not None else "")
                )
                if same:
                    return "same"
                return "conflict"
            self._mem[event["event_id"]] = event
            return True

    def get(self, event_id):
        if self._db_ok and self._conn:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT event_id, device_id, observed_at, type, note, received_at FROM events WHERE event_id=%s",
                    (event_id,),
                )
                row = cur.fetchone()
                cur.close()
                if not row:
                    return None
                return {
                    "event_id": row[0],
                    "device_id": row[1],
                    "observed_at": row[2],
                    "type": row[3],
                    "note": row[4],
                    "received_at": row[5],
                }
            except Exception:
                return False
        with self._lock:
            return self._mem.get(event_id)

    def latest(self, limit=MAX_LIST):
        if self._db_ok and self._conn:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT event_id, device_id, observed_at, type, note, received_at FROM events ORDER BY received_at DESC, event_id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
                cur.close()
                out = []
                for row in rows:
                    out.append(
                        {
                            "event_id": row[0],
                            "device_id": row[1],
                            "observed_at": row[2],
                            "type": row[3],
                            "note": row[4],
                            "received_at": row[5],
                        }
                    )
                return out
            except Exception:
                return None
        with self._lock:
            items = list(self._mem.values())
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

        def send_json(self, status, payload):
            return self._send(status, payload)

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

        def send_error_json(self, status, error, field):
            self.send_json(status, {"error": error, "field": field})

        def authenticate(self, required_role):
            reporter_token = os.environ.get("REPORTER_TOKEN") or None
            operator_token = os.environ.get("OPERATOR_TOKEN") or None
            authorization = self.headers.get("Authorization", "")
            scheme, separator, token = authorization.partition(" ")
            if scheme != "Bearer" or not separator or not token:
                self.send_error_json(401, "authentication_required", "authorization")
                return False

            roles = set()
            token_bytes = token.encode("utf-8")
            if reporter_token and hmac.compare_digest(token_bytes, reporter_token.encode("utf-8")):
                roles.add("reporter")
            if operator_token and hmac.compare_digest(token_bytes, operator_token.encode("utf-8")):
                roles.add("operator")
            if not roles:
                self.send_error_json(401, "invalid_token", "authorization")
                return False
            if len(roles) > 1:
                self.send_error_json(403, "forbidden", "authorization")
                return False
            if required_role not in roles:
                self.send_error_json(403, "forbidden", "authorization")
                return False
            return True

        @staticmethod
        def decode_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        @staticmethod
        def validate_event(event):
            if not isinstance(event, dict):
                return "body"
            extra_fields = set(event) - EVENT_FIELDS
            if extra_fields:
                return sorted(extra_fields)[0]
            missing_fields = REQUIRED_EVENT_FIELDS - set(event)
            if missing_fields:
                return sorted(missing_fields)[0]

            event_id = event["event_id"]
            if not isinstance(event_id, str) or not 1 <= len(event_id) <= 64 \
                    or not EVENT_ID_PATTERN.fullmatch(event_id):
                return "event_id"
            device_id = event["device_id"]
            if not isinstance(device_id, str) or not 1 <= len(device_id) <= 32 \
                    or not EVENT_ID_PATTERN.fullmatch(device_id):
                return "device_id"
            observed_at = event["observed_at"]
            if not isinstance(observed_at, str):
                return "observed_at"
            timestamp = observed_at[:-1] + "+00:00" if observed_at.endswith(("Z", "z")) else observed_at
            try:
                parsed_time = datetime.fromisoformat(timestamp)
            except ValueError:
                return "observed_at"
            if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
                return "observed_at"
            if not isinstance(event["type"], str) or event["type"] not in EVENT_TYPES:
                return "type"
            if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > MAX_NOTE):
                return "note"
            return None

        def read_event_body(self):
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                self.send_error_json(400, "invalid_content_type", "content_type")
                return None
            try:
                content_length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error_json(400, "invalid_content_length", "content_length")
                return None
            if content_length < 0 or content_length > MAX_BODY_BYTES:
                self.send_error_json(400, "invalid_body_size", "body")
                return None
            try:
                raw_body = self.rfile.read(content_length)
                event = json.loads(raw_body.decode("utf-8"), object_pairs_hook=self.decode_object)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self.send_error_json(400, "invalid_json", "body")
                return None
            field = self.validate_event(event)
            if field:
                self.send_error_json(400, "invalid_event", field)
                return None
            return event

        def _error(self, code, error, field=None):
            body = {"error": error}
            if field is not None:
                body["field"] = field
            return self._send(code, body)

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
                                        "auth_configured": self._auth_configured(), "db_configured": getattr(store, "db_configured", lambda: False)()})
            if route == "/events":
                if not self.authenticate("operator"):
                    return
                events = store.latest()
                if events is None:
                    return self._error(503, "database_unavailable", "database")
                return self._send(200, {"events": events})
            if route.startswith("/events/"):
                if not self.authenticate("operator"):
                    return
                event = store.get(urllib.parse.unquote(route[len("/events/"):]))
                if event is False:
                    return self._error(503, "database_unavailable", "database")
                if event is None:
                    return self._error(404, "not_found", "event_id")
                return self._send(200, event)
            return self._error(404, "not_found", "path")

        def do_POST(self):
            if self._route() != "/events":
                return self._error(404, "not_found", "path")
            if not self.authenticate("reporter"):
                return
            event = self.read_event_body()
            if event is None:
                return
            event["received_at"] = utc_now()
            res = store.add(event)
            if res == "conflict":
                return self._error(409, "duplicate_event_id", "event_id")
            if res == "same":
                return self._send(200, event)
            if res is False:
                return self._error(503, "database_unavailable", "database")
            return self._send(201, event)

        def do_DELETE(self):
            return self._error(405, "method_not_allowed", "method")

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    return server


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
