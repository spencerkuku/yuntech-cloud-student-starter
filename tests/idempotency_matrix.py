#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / ".local" / "resources.json"
SECRET = ROOT / ".local" / "app.env"


def get_host():
    sys.path.insert(0, str(ROOT / "scripts"))
    from lab import context, run_aws

    try:
        from resources import load_deployment_target
        target = load_deployment_target(RES)
        instance_id = target["instance_id"]
        row = run_aws(["ec2", "describe-instances", "--instance-ids", instance_id,
                       "--query", "Reservations[0].Instances[0].{S:State.Name,P:PublicIpAddress}"],
                      context()["region"])
    except Exception as exc:
        print(f"host lookup failed: {type(exc).__name__}", file=sys.stderr)
        return None, None
    if row.get("S") != "running" or not row.get("P"):
        return None, instance_id
    return row["P"], instance_id


def get_tokens():
    values = {}
    try:
        for line in SECRET.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    except OSError:
        pass
    return values.get("REPORTER_TOKEN", ""), values.get("OPERATOR_TOKEN", "")


def curl_json(url, method="GET", headers=None, data=None, timeout=10):
    request_headers = dict(headers or {})
    body = json.dumps(data).encode("utf-8") if data is not None else None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status, payload = response.status, response.read()
    except HTTPError as error:
        status, payload = error.code, error.read()
    except (OSError, URLError, TimeoutError):
        return 0, {}
    try:
        parsed = json.loads(payload.decode("utf-8")) if payload else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
    return status, parsed


def redact(value, secrets_to_redact):
    if isinstance(value, dict):
        return {redact(key, secrets_to_redact): redact(item, secrets_to_redact)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets_to_redact) for item in value]
    if isinstance(value, str):
        for secret in secrets_to_redact:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def matrix_passed(health_status, health, rows):
    return (
        health_status == 200
        and isinstance(health, dict)
        and isinstance(health.get("version"), str)
        and bool(health["version"])
        and health.get("db_configured") is True
        and len(rows) == 5
        and all(row.get("passed") is True for row in rows)
    )


def main():
    ip, instance_id = get_host()
    if not ip:
        print("STOP: no running W3 host recorded in .local/resources.json")
        return 1
    base = f"http://{ip}"
    reporter, operator = get_tokens()
    if not reporter or not operator:
        print("STOP: reporter/operator tokens are unavailable in local settings")
        return 1
    health_status, health = curl_json(f"{base}/health")
    version = health.get("version", "") if isinstance(health, dict) else ""
    db_configured = health.get("db_configured", "") if isinstance(health, dict) else ""
    print(f"health: version={version} db_configured={db_configured}")
    if health_status != 200 or db_configured is not True:
        print("STOP: health check did not confirm a configured database")
        return 1

    event_id = f"test-{uuid.uuid4().hex[:16]}"
    event = {"event_id": event_id, "device_id": "dev-001",
             "observed_at": "2026-10-06T10:00:00Z", "type": "status", "note": "hello"}
    headers = {"Authorization": f"Bearer {reporter}"}
    rows = []

    def record(number, name, expected, status, body, check=None):
        safe_body = redact(body, (reporter, operator))
        passed = status == expected and (check is None or check(body))
        rows.append({"number": number, "expected": expected,
                     "status": status, "passed": passed})
        print(f"#{number} {name} -> {status} {json.dumps(safe_body, ensure_ascii=False, sort_keys=True)}")
        return passed

    code, body = curl_json(f"{base}/events", "POST", headers, event)
    record(1, "POST new", 201, code, body,
           lambda result: isinstance(result, dict)
           and result.get("event_id") == event_id and bool(result.get("received_at")))
    code, body = curl_json(f"{base}/events", "POST", headers, event)
    record(2, "POST same", 200, code, body,
           lambda result: isinstance(result, dict) and result.get("event_id") == event_id)
    changed = {**event, "note": "changed"}
    code, body = curl_json(f"{base}/events", "POST", headers, changed)
    record(3, "POST changed content", 409, code, body)

    config_values = {}
    config_path = ROOT / ".local" / "config"
    if config_path.is_file():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key_name, value = line.split("=", 1)
                config_values[key_name.strip()] = value.strip()
    owner = config_values.get("OWNER", "")
    from resources import load_deployment_target
    target = load_deployment_target(RES)
    key_path = Path.home() / ".ssh" / target["key_name"]
    key = str(key_path) if key_path.is_file() and not key_path.is_symlink() else None
    if key:
        restart = subprocess.run(
            ["ssh", "-i", key, "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10", f"ec2-user@{ip}",
             "sudo systemctl restart inspection"],
            capture_output=True, text=True, timeout=30)
    else:
        restart = None
    if restart is not None and restart.returncode == 0:
        ready = False
        for _ in range(15):
            status, health_after = curl_json(f"{base}/health", timeout=2)
            if status == 200 and health_after.get("db_configured") is True:
                ready = True
                break
            time.sleep(2)

        if ready:
            code, body = curl_json(
                f"{base}/events/{event_id}",
                headers={"Authorization": f"Bearer {operator}"}
            )
        else:
            code, body = 0, {}
    else:
        code, body = 0, {}
    record(4, "GET after service restart", 200, code, body,
           lambda result: isinstance(result, dict) and result.get("event_id") == event_id)

    count = "?"
    if key and restart is not None and restart.returncode == 0:
        remote_script = f"""#!/bin/bash
set -a
. /etc/inspection/app.env
set +a
EVENT_ID={event_id!r}
PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \\
    -v event_id="$EVENT_ID" <<'SQL'
SELECT count(*) FROM events WHERE event_id = :'event_id';
SQL
"""
        result = subprocess.run(
            ["ssh", "-i", key, "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=15", f"ec2-user@{ip}", "sudo bash -s"],
            input=remote_script, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            numeric = [line for line in lines if line.isdigit()]
            count = numeric[-1] if numeric else "unavailable"
        record(5, f"psql count(event_id={event_id})", 0,
            0 if count.isdigit() else 1, {"count": count},
            lambda result: result.get("count") == "1")
    return 0 if matrix_passed(health_status, health, rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
