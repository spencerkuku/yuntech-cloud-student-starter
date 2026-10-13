#!/usr/bin/env python3
"""Local reconciliation helper for the W6 IoT sprint."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
RESOURCES = LOCAL / "resources.json"
APP_ENV = LOCAL / "app.env"


def load_json(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_env(path: Path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def load_health():
    data = load_json(RESOURCES)
    host = data.get("host_public_ip") or data.get("public_ip")
    if not host:
        raise RuntimeError("host_address_missing")
    request = Request(f"http://{host}/health", method="GET")
    with urlopen(request, timeout=8) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def load_last_run(prefix):
    state = LOCAL / "w06-last-run.json"
    if not state.exists():
        raise RuntimeError("run_marker_missing")
    data = json.loads(state.read_text(encoding="utf-8"))
    if data.get("prefix") != prefix:
        raise RuntimeError("run_marker_prefix_mismatch")
    return data


def ssh_command(host, remote_command, stdin_path=None):
    command = [
        "ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", os.path.expanduser("~/.ssh/id_ed25519"),
        "ec2-user@" + host,
        remote_command,
    ]
    if stdin_path is None:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    else:
        with stdin_path.open("rb") as stream:
            result = subprocess.run(command, stdin=stream, capture_output=True, text=True, check=False)
    if result.returncode:
        marker = next(
            (line for line in result.stdout.splitlines() if line.startswith("DB_COUNT_ERROR=")),
            "",
        )
        raise RuntimeError(marker or "ssh_command_failed")
    return result.stdout.strip()


def consumer_counts(host, prefix):
    remote = (
        "sudo journalctl -u inspection-consumer -o cat --no-pager "
        f"| grep -F 'event_id={prefix}-' "
        "| grep -o 'result=[a-z]*' | sort | uniq -c"
    )
    output = ssh_command(host, remote)
    counts = {"created": 0, "duplicate": 0, "rejected": 0, "error": 0}
    for line in output.splitlines():
        match = re.fullmatch(r"\s*(\d+)\s+result=(created|duplicate|rejected|error)\s*", line)
        if match:
            counts[match.group(2)] = int(match.group(1))
    return counts


def database_count(host, db_env_path, prefix):
    script = """import sys
values = {}
for line in sys.stdin:
    line = line.rstrip("\\n")
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
connection = None
try:
    import psycopg2
    connection = psycopg2.connect(
        host=values["DB_HOST"], dbname=values["DB_NAME"],
        user=values["DB_USER"], password=values["DB_PASSWORD"],
        sslmode="verify-full", sslrootcert="/etc/inspection/rds-ca.pem",
        connect_timeout=8,
    )
    connection.set_session(readonly=True, autocommit=True)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM events WHERE left(event_id, length(%s)) = %s",
            (sys.argv[1], sys.argv[1]),
        )
        print("DB_COUNT=" + str(cursor.fetchone()[0]))
except Exception as exc:
    print("DB_COUNT_ERROR=" + type(exc).__name__)
    sys.exit(1)
finally:
    if connection is not None:
        connection.close()
"""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    remote = f"sudo /usr/bin/python3 -c 'import base64;exec(base64.b64decode(\"{encoded}\"))' {prefix + '-'}"
    output = ssh_command(host, remote, stdin_path=db_env_path)
    match = re.search(r"^DB_COUNT=(\d+)$", output, re.MULTILINE)
    if not match:
        raise RuntimeError("db_count_missing")
    return int(match.group(1))


def filtered_events(host, code, prefix, operator_token):
    query = urlencode({"device_id": f"{code}-d01"})
    request = Request(
        f"http://{host}/events?{query}",
        headers={"Authorization": f"Bearer {operator_token}"},
        method="GET",
    )
    with urlopen(request, timeout=8) as response:
        rows = json.loads(response.read().decode("utf-8"))
    matching = [row for row in rows if str(row.get("event_id", "")).startswith(prefix + "-")]
    return matching


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    code = args.code
    run = args.run
    prefix = f"{code}-{run}"
    if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", code) or not re.fullmatch(r"[A-Za-z0-9-]{1,40}", run):
        raise SystemExit("prefix must match ^[A-Za-z0-9-]{1,40}$")

    try:
        status, health = load_health()
        if status != 200:
            raise RuntimeError("health_http_error")
        host = load_json(RESOURCES).get("host_public_ip") or load_json(RESOURCES).get("public_ip")
        if not host:
            raise RuntimeError("host_address_missing")
        env = load_env(APP_ENV)
        operator_token = env.get("OPERATOR_TOKEN", "")
        if not operator_token:
            raise RuntimeError("operator_config_missing")

        last = load_last_run(prefix)
        print(f"health_version={health.get('version')} db_configured={health.get('db_configured')} prefix={prefix}")
        print(f"puback_ok={last.get('puback_ok', 0)}/32")

        filtered = filtered_events(host, code, prefix, operator_token)
        print(f"device_rows={len(filtered)} sample={','.join(row.get('event_id', '') for row in filtered[:3])}")

        counts = {"created": 0, "duplicate": 0, "rejected": 0, "error": 0}
        db_rows = 0
        complete = False
        for attempt in range(12):
            counts = consumer_counts(host, prefix)
            db_rows = database_count(host, LOCAL / "db.env", prefix + "-")
            if sum(counts.values()) >= 32 and db_rows >= 30:
                complete = True
                break
            if attempt < 11:
                time.sleep(5)

        print(
            "consumer_counts="
            f"created:{counts['created']},duplicate:{counts['duplicate']},"
            f"rejected:{counts['rejected']},error:{counts['error']}"
        )
        print(f"consumer_total={sum(counts.values())}/32")
        print(f"database_rows={db_rows}")

        evidence_command = (
            "sudo journalctl -u inspection-consumer -o cat --no-pager "
            f"| grep -E 'event_id={prefix}-d(01-01|99-01) '")
        evidence = ssh_command(host, evidence_command)
        print("consumer_evidence:")
        print(evidence or "(no matching lines)")

        passed = (
            complete
            and health.get("db_configured") is True
            and last.get("puback_ok") == 32
            and counts == {"created": 30, "duplicate": 1, "rejected": 1, "error": 0}
            and db_rows == 30
            and len(filtered) == 3
        )
        print("reconciliation=PASS" if passed else "reconciliation=FAIL")
        if not passed:
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"reconciliation=BLOCKED:{type(exc).__name__}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
