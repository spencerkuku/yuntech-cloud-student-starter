#!/usr/bin/env python3
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".local" / "app.env"
RESOURCES_FILE = ROOT / ".local" / "resources.json"

def load_env():
    result = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result

def request(method, url, token=None, body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None

    req = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

env = load_env()
reporter = env["REPORTER_TOKEN"]
operator = env["OPERATOR_TOKEN"]

resources = json.loads(RESOURCES_FILE.read_text())
instance_id = resources["instance_id"]

# Public IP 由參數傳入，避免在程式中硬寫。
if len(sys.argv) != 2:
    raise SystemExit("Usage: python3 tests/run_w04_matrix.py <PUBLIC_IP>")

base = f"http://{sys.argv[1]}"

valid = {
    "event_id": "g02-cookieI-0001",
    "device_id": "g02-d01",
    "observed_at": "2026-09-29T10:00:00+08:00",
    "type": "status",
    "note": "inspection normal",
}

invalid_time = {
    "event_id": "g02-cookieI-0002",
    "device_id": "g02-d01",
    "observed_at": "2026-09-29T10:00:00",
    "type": "anomaly",
    "note": "timezone missing",
}

rows = []

rows.append(("1 Reporter POST valid event",
             *request("POST", base + "/events", reporter, valid)))

rows.append(("2 POST without token",
             *request("POST", base + "/events", None, valid)))

rows.append(("3 Operator POST event",
             *request("POST", base + "/events", operator, valid)))

rows.append(("4 Reporter POST invalid timezone",
             *request("POST", base + "/events", reporter, invalid_time)))

rows.append(("5 Reporter POST duplicate event",
             *request("POST", base + "/events", reporter, valid)))

rows.append(("6 Reporter GET /events",
             *request("GET", base + "/events", reporter)))

rows.append(("7 Operator GET /events",
             *request("GET", base + "/events", operator)))

expected = [201, 401, 403, 400, 409, 403, 200]

# 取得目前部署版本
health_status, health_body = request("GET", base + "/health")
try:
    health = json.loads(health_body)
    version = health.get("version", "UNKNOWN")
except json.JSONDecodeError:
    version = "UNKNOWN"

print()
print("W4 rejection matrix")
print("version:", version)
print("=" * 72)

all_ok = True

for i, ((name, status, body), want) in enumerate(zip(rows, expected), 1):
    ok = status == want

    if i == 7 and ok:
        try:
            payload = json.loads(body)
            events = payload.get("events", [])
            ok = any(
                event.get("event_id") == valid["event_id"]
                for event in events
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            ok = False

    result = "PASS" if ok else "FAIL"

    try:
        parsed = json.loads(body)
        display_body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        display_body = body.strip()

    print(f"#{i} {name}")
    print(f"HTTP {status}  expected={want}  {result}")
    print(f"body: {display_body}")
    print("-" * 72)

    all_ok &= ok

print("RESULT:", "ALL PASS" if all_ok else "FAILED")

raise SystemExit(0 if all_ok else 1)
