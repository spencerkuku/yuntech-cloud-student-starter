#!/usr/bin/env python3
"""Reconcile the W6 gateway, consumer, database, and filtered API counts."""
import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def env_file(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    if not CODE_RE.fullmatch(args.code) or not CODE_RE.fullmatch(args.run):
        raise SystemExit("code and run must match ^[A-Za-z0-9-]{1,40}$")
    prefix = f"{args.code}-{args.run}-"
    resources = json.loads((ROOT / ".local" / "resources.json").read_text(encoding="utf-8"))
    settings = env_file(ROOT / ".local" / "w6.env")
    app = env_file(ROOT / ".local" / "app.env")
    host = resources.get("host_public_ip") or resources.get("public_ip")
    if not host:
        raise SystemExit("resources.json must contain the current host public IP")
    health = json.loads(urlopen(f"http://{host}/health", timeout=8).read())
    print(json.dumps({
        "version": health.get("version"), "db_configured": health.get("db_configured"),
        "prefix": prefix[:-1],
    }))
    last_run = json.loads((ROOT / ".local" / "w06-last-run.json").read_text(encoding="utf-8"))
    if last_run.get("prefix") != prefix[:-1]:
        raise SystemExit("last-run prefix does not match")
    print(f"puback_ok={last_run.get('puback_ok')}/32")
    ssh = [
        "ssh", "-i", os.path.expanduser(settings["SSH_KEY_FILE"]),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        f"{settings['SSH_USER']}@{host}",
    ]
    log_command = (
        "sudo journalctl -u inspection-consumer -o cat --no-pager | "
        f"grep -F {json.dumps('event_id=' + prefix)} | "
        "grep -o 'result=[a-z]*' | sort | uniq -c"
    )
    db_command = (
        "sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; "
        "PGPASSWORD=\"$DB_PASSWORD\" psql "
        "\"host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full "
        "sslrootcert=/etc/inspection/rds-ca.pem\" "
        f"-v prefix={json.dumps(prefix)}' <<'SQL'\n"
        "SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';\n"
        "SQL"
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        logs = subprocess.run(ssh + [log_command], capture_output=True, text=True, check=False).stdout
        counts = dict(re.findall(r"\s*(\d+)\s+result=(created|duplicate|rejected|error)", logs))
        total = sum(int(value) for value in counts.values())
        if total >= 32:
            break
        time.sleep(5)
    for result in (("consumer", logs), ("database", subprocess.run(
        ssh + [db_command], capture_output=True, text=True, check=False
    ).stdout)):
        print(f"{result[0]}={result[1].strip()}")
    request = Request(
        f"http://{host}/events?device_id={args.code}-d01",
        headers={"Authorization": "Bearer " + app["OPERATOR_TOKEN"]},
    )
    events = json.loads(urlopen(request, timeout=8).read())["events"]
    selected = [event["event_id"] for event in events if event["event_id"].startswith(prefix)]
    print(f"filtered_count={len(selected)}")
    for event_id in selected:
        print(event_id)


if __name__ == "__main__":
    main()
