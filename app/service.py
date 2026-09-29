#!/usr/bin/env python3
"""W4 inspection service: health, authenticated event API, and display page."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
ALLOWED_TYPES = {"status", "anomaly", "test"}
ALLOWED_FIELDS = {"event_id", "device_id", "observed_at", "type", "note"}
REQUIRED_FIELDS = {"event_id", "device_id", "observed_at", "type"}
MAX_BODY_BYTES = 4096


def make_server(version_file, port=8080):
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")

    started = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    reporter_token = os.environ.get("REPORTER_TOKEN", "")
    operator_token = os.environ.get("OPERATOR_TOKEN", "")
    auth_configured = bool(reporter_token and operator_token)

    # W4 intentionally stores events only in memory.
    events = []
    event_ids = set()

    def token_role(header):
        if not header or not header.startswith("Bearer "):
            return None

        token = header[len("Bearer "):]

        if reporter_token and hmac.compare_digest(token, reporter_token):
            return "reporter"

        if operator_token and hmac.compare_digest(token, operator_token):
            return "operator"

        return None

    def validate_event(event):
        if not isinstance(event, dict):
            return "invalid_json_object", "body"

        extra = set(event) - ALLOWED_FIELDS
        if extra:
            return "unexpected_field", sorted(extra)[0]

        for field in REQUIRED_FIELDS:
            if field not in event:
                return "missing_field", field

        event_id = event["event_id"]
        if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
            return "invalid_field", "event_id"

        device_id = event["device_id"]
        if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
            return "invalid_field", "device_id"

        observed_at = event["observed_at"]
        if not isinstance(observed_at, str):
            return "invalid_field", "observed_at"

        try:
            parsed_time = datetime.fromisoformat(
                observed_at.replace("Z", "+00:00")
            )
        except ValueError:
            return "invalid_field", "observed_at"

        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            return "invalid_field", "observed_at"

        if event["type"] not in ALLOWED_TYPES:
            return "invalid_field", "type"

        if "note" in event:
            note = event["note"]
            if not isinstance(note, str) or len(note) > 200:
                return "invalid_field", "note"

        return None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def send_json(self, status, body):
            data = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_error_json(self, status, error, field=""):
            self.send_json(
                status,
                {
                    "error": error,
                    "field": field,
                },
            )

        def authenticate(self, required_role):
            role = token_role(self.headers.get("Authorization"))

            if role is None:
                self.send_error_json(401, "unauthorized", "authorization")
                return False

            if role != required_role:
                self.send_error_json(403, "forbidden", "authorization")
                return False

            return True

        def do_GET(self):
            path = urlsplit(self.path).path

            if path == "/health":
                self.send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "inspection",
                        "version": version,
                        "started_at": started,
                        "auth_configured": auth_configured,
                    },
                )
                return

            if path == "/":
                self.send_page()
                return

            if path == "/events":
                if not self.authenticate("operator"):
                    return

                self.send_json(
                    200,
                    {
                        "events": list(reversed(events[-50:])),
                    },
                )
                return

            if path.startswith("/events/"):
                if not self.authenticate("operator"):
                    return

                event_id = unquote(path[len("/events/"):])

                if "/" in event_id or not event_id:
                    self.send_error_json(404, "not_found", "event_id")
                    return

                for event in events:
                    if event["event_id"] == event_id:
                        self.send_json(200, event)
                        return

                self.send_error_json(404, "not_found", "event_id")
                return

            self.send_error_json(404, "not_found", "path")

        def do_POST(self):
            path = urlsplit(self.path).path

            if path != "/events":
                self.send_error_json(404, "not_found", "path")
                return

            # Required review order:
            # 401 -> 403 -> 400 -> 409 -> 201
            if not self.authenticate("reporter"):
                return

            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                self.send_error_json(400, "invalid_content_type", "content_type")
                return

            content_length_text = self.headers.get("Content-Length")

            try:
                content_length = int(content_length_text)
            except (TypeError, ValueError):
                self.send_error_json(400, "invalid_body", "body")
                return

            if content_length < 0 or content_length > MAX_BODY_BYTES:
                self.send_error_json(400, "body_too_large", "body")
                return

            raw = self.rfile.read(content_length)

            if len(raw) > MAX_BODY_BYTES:
                self.send_error_json(400, "body_too_large", "body")
                return

            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_error_json(400, "invalid_json", "body")
                return

            validation_error = validate_event(event)
            if validation_error:
                error, field = validation_error
                self.send_error_json(400, error, field)
                return

            if event["event_id"] in event_ids:
                self.send_error_json(409, "duplicate_event", "event_id")
                return

            stored_event = dict(event)
            stored_event["received_at"] = (
                datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )

            events.append(stored_event)
            event_ids.add(stored_event["event_id"])

            self.send_json(201, stored_event)

        def send_page(self):
            page = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Inspection Events</title>
</head>
<body>
  <h1>Inspection Events</h1>

  <label for="token">Operator token</label>
  <input id="token" type="password" autocomplete="off">
  <button id="load" type="button">Load events</button>

  <p id="status"></p>
  <pre id="events"></pre>

  <script>
    const tokenInput = document.getElementById("token");
    const statusNode = document.getElementById("status");
    const eventsNode = document.getElementById("events");

    document.getElementById("load").addEventListener("click", async () => {
      const token = tokenInput.value;

      statusNode.textContent = "Loading...";
      eventsNode.textContent = "";

      try {
        const response = await fetch("/events", {
          method: "GET",
          headers: {
            "Authorization": "Bearer " + token
          }
        });

        const data = await response.json();

        if (!response.ok) {
          statusNode.textContent = "Request failed: HTTP " + response.status;
          return;
        }

        statusNode.textContent = "HTTP 200";
        eventsNode.textContent = JSON.stringify(data.events, null, 2);
      } catch (error) {
        statusNode.textContent = "Request failed";
      }
    });
  </script>
</body>
</html>
"""
            data = page.encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            # Never log paths, request bodies, headers, tokens, or events.
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(
        Path(__file__).with_name("version")
    ).serve_forever()