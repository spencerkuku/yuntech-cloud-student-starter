#!/usr/bin/env python3
"""W3 supplied inspection-service prototype; extend routes in later Sprints."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse


EVENT_STORE = {}
TOKENS = {"REPORTER_TOKEN": "reporter-token", "OPERATOR_TOKEN": "operator-token"}


def load_env(env=None):
    if env is None:
        env = os.environ
    TOKENS["REPORTER_TOKEN"] = env.get("REPORTER_TOKEN", TOKENS["REPORTER_TOKEN"])
    TOKENS["OPERATOR_TOKEN"] = env.get("OPERATOR_TOKEN", TOKENS["OPERATOR_TOKEN"])
    return TOKENS


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sample_event(event_id, device_id, observed_at, event_type, note=None):
    event = {
        "event_id": event_id,
        "device_id": device_id,
        "observed_at": observed_at,
        "type": event_type,
    }
    if note is not None:
        event["note"] = note
    return event


def _validate_event(payload):
    if not isinstance(payload, dict):
        return False, "body", "invalid_json_object"
    for key in ("event_id", "device_id", "observed_at", "type"):
        if key not in payload:
            return False, key, "required"
    if not isinstance(payload["event_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload["event_id"]):
        return False, "event_id", "invalid"
    if not isinstance(payload["device_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", payload["device_id"]):
        return False, "device_id", "invalid"
    if not isinstance(payload["observed_at"], str):
        return False, "observed_at", "invalid"
    try:
        datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
    except ValueError:
        return False, "observed_at", "invalid"
    if payload["observed_at"].endswith("Z"):
        pass
    elif "T" not in payload["observed_at"] or "+" not in payload["observed_at"] and "-" not in payload["observed_at"].split("T")[-1]:
        return False, "observed_at", "invalid"
    if payload["type"] not in {"status", "anomaly", "test"}:
        return False, "type", "invalid"
    if "note" in payload and (not isinstance(payload["note"], str) or len(payload["note"]) > 200):
        return False, "note", "invalid"
    return True, None, None


def _auth_token(headers):
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


def _json_error(message, field=None, status=400):
    payload = {"error": message}
    if field is not None:
        payload["field"] = field
    return status, payload


def _event_sort_key(event):
    value = event.get("received_at", "1970-01-01T00:00:00Z")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, timezone.utc)


def make_server(version_file, port=8080):
    load_env()
    version = Path(version_file).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("version must contain the deployed 40-character Git commit SHA")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def _send_json(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self):
            page = """
            <!doctype html>
            <html>
              <head><meta charset="utf-8"><title>Inspection</title></head>
              <body>
                <h1>Inspection events</h1>
                <label>Operator token <input id="token" type="password" /></label>
                <label>Device ID <input id="device" type="text" /></label>
                <button id="load">Read</button>
                <pre id="output"></pre>
                <script>
                  const tokenInput = document.getElementById('token');
                  const deviceInput = document.getElementById('device');
                  const output = document.getElementById('output');
                  document.getElementById('load').addEventListener('click', async () => {
                    const token = tokenInput.value.trim();
                    const device = deviceInput.value.trim();
                    const params = new URLSearchParams();
                    if (device) params.set('device_id', device);
                    const response = await fetch('/events?' + params.toString(), {
                      headers: { 'Authorization': 'Bearer ' + token }
                    });
                    const body = await response.json();
                    output.textContent = JSON.stringify(body, null, 2);
                  });
                </script>
              </body>
            </html>
            """
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return None, _json_error("missing_body", "body", 400)
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return None, _json_error("invalid_content_type", "Content-Type", 400)
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                return None, _json_error("invalid_json", "body", 400)
            if not isinstance(body, dict):
                return None, _json_error("invalid_json_object", "body", 400)
            return body, None

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query)

            if path == "/health":
                self._send_json(200, {
                    "status": "ok",
                    "service": "inspection",
                    "version": version,
                    "started_at": started,
                    "auth_configured": bool(TOKENS["REPORTER_TOKEN"]) and bool(TOKENS["OPERATOR_TOKEN"]),
                    "db_configured": False,
                })
                return

            if path == "/":
                self._send_html()
                return

            if path == "/events":
                token = _auth_token(self.headers)
                if token != TOKENS["OPERATOR_TOKEN"]:
                    self._send_json(401, {"error": "unauthorized", "field": "Authorization"})
                    return
                device_id = params.get("device_id", [None])[0]
                rows = list(EVENT_STORE.values())
                if device_id:
                    rows = [row for row in rows if row.get("device_id") == device_id]
                rows = sorted(rows, key=_event_sort_key, reverse=True)[:50]
                self._send_json(200, rows)
                return

            if path.startswith("/events/"):
                token = _auth_token(self.headers)
                if token != TOKENS["OPERATOR_TOKEN"]:
                    self._send_json(401, {"error": "unauthorized", "field": "Authorization"})
                    return
                event_id = path.split("/events/", 1)[1]
                row = EVENT_STORE.get(event_id)
                if row is None:
                    self._send_json(404, {"error": "not_found", "field": "event_id"})
                    return
                self._send_json(200, row)
                return

            self._send_json(404, {"error": "not_found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/events":
                token = _auth_token(self.headers)
                if token != TOKENS["REPORTER_TOKEN"]:
                    self._send_json(401, {"error": "unauthorized", "field": "Authorization"})
                    return
                payload, err = self._read_json_body()
                if err is not None:
                    self._send_json(err[0], err[1])
                    return
                ok, field, reason = _validate_event(payload)
                if not ok:
                    self._send_json(400, {"error": reason, "field": field})
                    return

                existing = EVENT_STORE.get(payload["event_id"])
                if existing is not None:
                    if existing == {**payload, "received_at": existing["received_at"]}:
                        self._send_json(200, existing)
                        return
                    self._send_json(409, {"error": "duplicate_event_id", "field": "event_id"})
                    return

                event = {**payload, "received_at": _utc_now_iso()}
                EVENT_STORE[payload["event_id"]] = event
                self._send_json(201, event)
                return

            self._send_json(404, {"error": "not_found"})

        def log_message(self, fmt, *args):
            pass  # Never log request paths, bodies, headers or query strings.

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    make_server(Path(__file__).with_name("version")).serve_forever()
