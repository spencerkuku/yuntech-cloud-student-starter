#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / ".local" / "resources.json"
SECRET = ROOT / ".local" / "app.env"


def get_host():
    sys.path.insert(0, str(ROOT / "scripts"))
    from lab import context, run_aws

    try:
        resources = json.loads(RES.read_text(encoding="utf-8"))
        live = [row for row in resources["rounds"]
                if row.get("instance", {}).get("state") != "terminated"]
        instance_id = live[-1]["instance"]["id"]
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
    command = ["curl", "-sS", "--max-time", str(timeout), "-w", "|%{http_code}"]
    if method == "POST":
        command += ["-X", "POST", "-H", "Content-Type: application/json"]
    for key, value in (headers or {}).items():
        command += ["-H", f"{key}: {value}"]
    if data is not None:
        command += ["-d", json.dumps(data)]
    command.append(url)
    result = subprocess.run(command, capture_output=True, text=True)
    body, _, code = result.stdout.rpartition("|")
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = body
    return int(code) if code.isdigit() else 0, parsed


def main():
    ip, instance_id = get_host()
    if not ip:
        print(f"no running host (instance={instance_id})")
        return 1
    base = f"http://{ip}"
    reporter, operator = get_tokens()
    _, health = curl_json(f"{base}/health")
    version = health.get("version", "") if isinstance(health, dict) else ""
    db_configured = health.get("db_configured", "") if isinstance(health, dict) else ""
    print(f"health: version={version} db_configured={db_configured}")

    event_id = f"test-{uuid.uuid4().hex[:16]}"
    event = {"event_id": event_id, "device_id": "dev-001",
             "observed_at": "2026-10-06T10:00:00Z", "type": "status", "note": "hello"}
    headers = {"Authorization": f"Bearer {reporter}"}
    code, body = curl_json(f"{base}/events", "POST", headers, event)
    print(f"#1 POST new -> {code} {body}")
    code, body = curl_json(f"{base}/events", "POST", headers, event)
    print(f"#2 POST same -> {code} {body}")
    changed = {**event, "note": "changed"}
    code, body = curl_json(f"{base}/events", "POST", headers, changed)
    print(f"#3 POST conflict -> {code} {body}")

    key_files = sorted(Path.home().glob(".ssh/w03-*-key"))
    key = str(key_files[0]) if key_files else None
    if key:
        restart = subprocess.run(
            ["ssh", "-i", key, "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10", f"ec2-user@{ip}",
             "sudo systemctl restart inspection"],
            capture_output=True, text=True, timeout=30)
        if restart.returncode:
            print(f"restart failed: exit={restart.returncode}")
        time.sleep(2)
    code, body = curl_json(f"{base}/events/{event_id}",
                           headers={"Authorization": f"Bearer {operator}"})
    print(f"#4 GET after restart -> {code} {body}")

    count = "?"
    if key:
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
        output = result.stdout.strip() or result.stderr.strip()
        if output:
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            numeric = [line for line in lines if line.isdigit()]
            count = numeric[-1] if numeric else lines[-1]
    print(f"#5 psql count(event_id={event_id}) -> {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
