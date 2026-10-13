#!/usr/bin/env python3
"""Local reconciliation helper for the W6 IoT sprint."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
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


def load_health():
    data = load_json(RESOURCES)
    host = data.get("host_public_ip") or data.get("public_ip")
    if not host:
        raise SystemExit("Need a public IP in .local/resources.json")
    req = Request(f"http://{host}/health", method="GET")
    with urlopen(req, timeout=8) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def load_last_run(prefix):
    state = LOCAL / "w06-last-run.json"
    if not state.exists():
        return {"prefix": prefix, "puback_ok": 0}
    data = json.loads(state.read_text())
    if data.get("prefix") != prefix:
        return {"prefix": prefix, "puback_ok": 0}
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    code = args.code
    run = args.run
    prefix = f"{code}-{run}"
    if not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", prefix):
        raise SystemExit("prefix must match ^[A-Za-z0-9-]{1,40}$")

    status, health = load_health()
    last = load_last_run(prefix)
    print(f"health_version={health.get('version')} db_configured={health.get('db_configured')} prefix={prefix}")
    print(f"puback_ok={last.get('puback_ok', 0)}/32")

    env = load_json(APP_ENV)
    if isinstance(env, dict):
        token = env.get("OPERATOR_TOKEN")
        if token:
            req = Request(f"http://{(load_json(RESOURCES)).get('host_public_ip') or (load_json(RESOURCES)).get('public_ip')}/events?device_id={code}-d01", headers={"Authorization": f"Bearer {token}"}, method="GET")
            with urlopen(req, timeout=8) as response:
                rows = json.loads(response.read().decode("utf-8"))
                matching = [row for row in rows if str(row.get('event_id', '')).startswith(prefix)]
                print(f"device_rows={len(matching)} sample={','.join(row.get('event_id','') for row in matching[:3])}")

    # Poll the consumer log and database count using the same SSH procedure described in the lab.
    for _ in range(12):
        try:
            ssh = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-i",
                    os.path.expanduser("~/.ssh/id_ed25519"),
                    "ec2-user@" + str((load_json(RESOURCES)).get('host_public_ip') or (load_json(RESOURCES)).get('public_ip')),
                    "sudo journalctl -u inspection-consumer -o cat --no-pager | grep -F 'event_id={prefix}-' | grep -o 'result=[a-z]*' | sort | uniq -c",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if ssh.stdout.strip():
                print(ssh.stdout.strip())
                break
        except Exception:
            pass
        time.sleep(5)


if __name__ == "__main__":
    main()
