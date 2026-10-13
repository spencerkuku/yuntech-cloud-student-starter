#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def get_host():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from lab import context, run_aws
    resources = json.loads((ROOT / ".local" / "resources.json").read_text(encoding="utf-8"))
    live = [row for row in resources["rounds"] if row.get("instance", {}).get("state") != "terminated"]
    instance_id = live[-1]["instance"]["id"]
    row = run_aws(["ec2", "describe-instances", "--instance-ids", instance_id,
                   "--query", "Reservations[0].Instances[0].{S:State.Name,P:PublicIpAddress}"], context()["region"])
    return row.get("P"), instance_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    if not CODE_RE.fullmatch(args.code) or not CODE_RE.fullmatch(args.run):
        parser.error("code and run must match ^[A-Za-z0-9-]{1,40}$")
    prefix = f"{args.code}-{args.run}"
    ip, instance_id = get_host()
    if not ip:
        raise SystemExit(f"no running host (instance={instance_id})")
    token = read_env(ROOT / ".local" / "app.env")["OPERATOR_TOKEN"]
    health = json.loads(urllib.request.urlopen(f"http://{ip}/health", timeout=10).read())
    print(f"health: version={health.get('version')} db_configured={health.get('db_configured')}")
    print(f"prefix: {prefix}")
    last = json.loads((ROOT / ".local" / "w06-last-run.json").read_text(encoding="utf-8"))
    if last.get("prefix") != prefix:
        raise SystemExit("STOP: w06-last-run.json prefix does not match")
    print(f"puback_ok={last.get('puback_ok')}/32")
    key_files = sorted(Path.home().glob(".ssh/w03-*-key"))
    if not key_files:
        raise SystemExit("STOP: no W03 SSH key found")

    remote = r'''#!/bin/bash
set +e
PREFIX="$1-"
for attempt in $(seq 1 12); do
  counts=$(sudo journalctl -u inspection-consumer -o cat --no-pager | awk -v p="$PREFIX" '$0 ~ "event_id=" p {for (i=1;i<=NF;i++) if ($i ~ /^result=/) print $i}' | sort | uniq -c)
  created=$(printf '%s\n' "$counts" | awk '$2 == "result=created" {print $1+0}')
  duplicate=$(printf '%s\n' "$counts" | awk '$2 == "result=duplicate" {print $1+0}')
  rejected=$(printf '%s\n' "$counts" | awk '$2 == "result=rejected" {print $1+0}')
  error=$(printf '%s\n' "$counts" | awk '$2 == "result=error" {print $1+0}')
  created=${created:-0}; duplicate=${duplicate:-0}; rejected=${rejected:-0}; error=${error:-0}
    set -a
    . /etc/inspection/app.env
    set +a
    db=$(PGPASSWORD="$DB_PASSWORD" psql "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" -v prefix="$PREFIX" -At <<"SQL"
SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';
SQL
    2>/dev/null | tail -1)
  printf 'consumer: created=%s duplicate=%s rejected=%s error=%s db=%s\n' "$created" "$duplicate" "$rejected" "$error" "${db:-?}"
  total=$((created + duplicate + rejected + error))
  if [ "$total" -ge 32 ]; then exit 0; fi
  sleep 5
done
exit 1
'''
    result = subprocess.run(["ssh", "-i", str(key_files[0]), "-o", "StrictHostKeyChecking=accept-new",
                             "-o", "ConnectTimeout=15", f"ec2-user@{ip}", "sudo bash -s --", prefix],
                            input=remote, text=True, capture_output=True, timeout=75)
    print(result.stdout, end="")
    if result.returncode:
        raise SystemExit("STOP: consumer reconciliation timed out")
    query = urllib.parse.urlencode({"device_id": f"{args.code}-d01"})
    request = urllib.request.Request(f"http://{ip}/events?{query}",
                                     headers={"Authorization": "Bearer " + token})
    events = json.loads(urllib.request.urlopen(request, timeout=10).read())
    selected = [item["event_id"] for item in events if item["event_id"].startswith(prefix + "-")]
    print(f"device_filter: count={len(selected)} event_ids={','.join(selected)}")


if __name__ == "__main__":
    main()