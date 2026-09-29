#!/usr/bin/env python3
"""Run the seven-row W4 authorization and validation matrix against a service."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".local" / "app.env"
TIMEOUT_SECONDS = 8


def load_tokens(path):
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        raise ValueError("Cannot read the configured app.env file.") from None
    values = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {"REPORTER_TOKEN", "OPERATOR_TOKEN"} or key in values:
            raise ValueError("app.env must contain one value for each W4 token.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            raise ValueError("app.env must contain one value for each W4 token.")
        values[key] = value
    if set(values) != {"REPORTER_TOKEN", "OPERATOR_TOKEN"}:
        raise ValueError("app.env must contain one value for each W4 token.")
    return values["REPORTER_TOKEN"], values["OPERATOR_TOKEN"]


def request_json(base_url, method, path, body=None, token=None):
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            payload = response.read()
    except HTTPError as error:
        status = error.code
        payload = error.read()
    except (OSError, URLError):
        return None, None
    try:
        return status, json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def run_matrix(base_url, reporter_token, operator_token):
    health_status, health_body = request_json(base_url, "GET", "/health")
    health_version = health_body.get("version") if isinstance(health_body, dict) else None
    event_id = "g02-m4-" + secrets.token_hex(8)
    event = {
        "event_id": event_id,
        "device_id": "matrix-device",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "type": "test",
        "note": "W4 rejection matrix",
    }
    invalid_time_event = dict(event, event_id=event_id + "-no-zone",
                              observed_at="2026-09-29T10:00:00")

    rows = []

    def record(name, expected, method, path, body=None, token=None, check=None):
        status, payload = request_json(base_url, method, path, body, token)
        passed = status == expected and (check is None or check(payload))
        rows.append({
            "number": len(rows) + 1,
            "name": name,
            "expected": expected,
            "status": status,
            "body": payload,
            "passed": passed,
        })

    record("reporter POST #1", 201, "POST", "/events", event, reporter_token,
           lambda payload: isinstance(payload, dict)
           and payload.get("event_id") == event_id
           and payload.get("received_at"))
    record("no-token POST", 401, "POST", "/events", dict(event, event_id=event_id + "-no-token"))
    record("operator POST", 403, "POST", "/events", dict(event, event_id=event_id + "-operator"), operator_token)
    record("reporter POST without timezone", 400, "POST", "/events", invalid_time_event,
           reporter_token,
           lambda payload: isinstance(payload, dict) and payload.get("field") == "observed_at")
    record("reporter duplicate POST #1", 409, "POST", "/events", event, reporter_token)
    record("reporter GET /events", 403, "GET", "/events", token=reporter_token)
    record("operator GET /events includes #1", 200, "GET", "/events", token=operator_token,
           check=lambda payload: isinstance(payload, dict) and any(
               isinstance(item, dict) and item.get("event_id") == event_id
               for item in payload.get("events", [])))
    return {
        "health_status": health_status,
        "health_version": health_version,
        "rows": rows,
        "passed": health_status == 200 and isinstance(health_version, str)
        and all(row["passed"] for row in rows),
    }


def redact(value, tokens):
    if isinstance(value, dict):
        return {redact(key, tokens): redact(item, tokens) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, tokens) for item in value]
    if isinstance(value, str):
        for token in tokens:
            if token:
                value = value.replace(token, "[REDACTED]")
    return value


def format_report(result, tokens):
    health_version = redact(result["health_version"], tokens)
    lines = [f"Health version: {health_version}"]
    for row in result["rows"]:
        body = json.dumps(redact(row["body"], tokens), ensure_ascii=False, sort_keys=True)
        lines.append(f"{row['number']}. HTTP {row['status']} body={body}")
    return "\n".join(lines)


def validate_base_url(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname \
            or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Base URL must be an HTTP(S) origin without query or credentials.")
    return base_url.rstrip("/")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get(
        "INSPECTION_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args(argv)
    try:
        base_url = validate_base_url(args.base_url)
        reporter_token, operator_token = load_tokens(args.env_file)
    except ValueError as error:
        print(f"STOP: {error}", file=sys.stderr)
        return 2

    result = run_matrix(base_url, reporter_token, operator_token)
    print(format_report(result, (reporter_token, operator_token)))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
