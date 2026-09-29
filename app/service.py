#!/usr/bin/env python3
"""Small in-memory inspection service for W3/W4."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import unquote, urlsplit


MAX_BODY_BYTES = 4096
EVENT_FIELDS = {"event_id", "device_id", "observed_at", "type", "note"}
REQUIRED_EVENT_FIELDS = {"event_id", "device_id", "observed_at", "type"}
EVENT_TYPES = {"status", "anomaly", "test"}
EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


def make_server(version_file, port=8080):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    events = []
    events_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
            if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > 200):
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

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                self.send_json(200, {
                    "status": "ok",
                    "service": "inspection",
                    "version": version,
                    "started_at": started,
                    "auth_configured": bool(os.environ.get("REPORTER_TOKEN") and
                                            os.environ.get("OPERATOR_TOKEN")),
                })
                return
            if path == "/":
                page = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Inspection events</title>
<main><h1>Inspection events</h1>
<label>Operator token <input id="token" type="password" autocomplete="off"></label>
<button id="load" type="button">Load events</button><p id="status" role="status"></p>
<ol id="events"></ol></main>
<script>
const token = document.getElementById("token");
const status = document.getElementById("status");
const list = document.getElementById("events");
document.getElementById("load").addEventListener("click", async () => {
  list.replaceChildren();
  status.textContent = "Loading...";
  try {
    const response = await fetch("/events", {headers: {Authorization: `Bearer ${token.value}`}});
    const result = await response.json();
    if (!response.ok) {
      status.textContent = `Request failed: ${result.error}`;
      return;
    }
    status.textContent = `${result.events.length} event(s)`;
    for (const event of result.events) {
      const item = document.createElement("li");
            item.textContent = `${event.event_id} | ${event.device_id} | ${event.observed_at} | ${event.type} | ${event.received_at} | ${event.note || ""}`;
      list.appendChild(item);
    }
  } catch (_) {
    status.textContent = "Request failed";
  }
});
</script></html>"""
                data = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/events":
                if not self.authenticate("operator"):
                    return
                with events_lock:
                    latest_events = list(reversed(events[-50:]))
                self.send_json(200, {"events": latest_events})
                return
            path_parts = path.split("/")
            if len(path_parts) == 3 and path_parts[1] == "events" and path_parts[2]:
                if not self.authenticate("operator"):
                    return
                event_id = unquote(path_parts[2])
                with events_lock:
                    event = next((item for item in reversed(events)
                                  if item["event_id"] == event_id), None)
                if event is not None:
                    self.send_json(200, event)
                    return
                self.send_error_json(404, "not_found", "event_id")
                return
            self.send_error_json(404, "not_found", "path")

        def do_POST(self):
            if urlsplit(self.path).path != "/events":
                self.send_error_json(404, "not_found", "path")
                return
            if not self.authenticate("reporter"):
                return
            event = self.read_event_body()
            if event is None:
                return
            stored_event = dict(event)
            stored_event["received_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")
            with events_lock:
                if any(existing["event_id"] == event["event_id"] for existing in events):
                    self.send_error_json(409, "duplicate_event", "event_id")
                    return
                events.append(stored_event)
            self.send_json(201, stored_event)

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
