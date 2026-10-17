#!/usr/bin/env python3
"""Reconcile one W6 Gateway run against its consumer and API/database records."""
import argparse
from contextlib import redirect_stdout
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIR = ROOT / ".local"
RUN_FILE = LOCAL_DIR / "w06-last-run.json"
RESOURCES_FILE = LOCAL_DIR / "resources.json"
APP_ENV_FILE = LOCAL_DIR / "app.env"
REGION = "us-east-1"
SSH_USER = "ec2-user"
SSH_TIMEOUT = 20
HTTP_TIMEOUT = 8
POLL_INTERVAL = 5
POLL_TIMEOUT = 60
PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,40}$")
RUN_PATTERN = re.compile(r"^r[1-9][0-9]*$")
RESULT_NAMES = ("created", "duplicate", "rejected", "error")

REMOTE_QUERY = r"""set -euo pipefail
prefix="$1"
printf '%s\n' COUNTS_BEGIN
sudo journalctl -u inspection-consumer -o cat --no-pager \
  | grep -F -- "event_id=${prefix}-" \
  | grep -o 'result=[a-z]*' \
  | sort \
  | uniq -c || true
printf '%s\n' COUNTS_END
db_count="$(sudo bash -s -- "${prefix}-" <<'DBSCRIPT'
set -euo pipefail
set -a
. /etc/inspection/app.env
set +a
PGPASSWORD="$DB_PASSWORD" psql -X -qAt \
  "host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem" \
  -v ON_ERROR_STOP=1 -v prefix="$1" <<'SQL'
SELECT count(*) FROM events WHERE left(event_id, length(:'prefix')) = :'prefix';
SQL
DBSCRIPT
)"
printf 'DBCOUNT=%s\n' "$db_count"
"""


class ReconcileError(Exception):
    """Safe-to-display validation or operational error."""


def build_prefix(code, run):
    if not isinstance(code, str) or not PREFIX_PATTERN.fullmatch(code):
        raise ReconcileError("Invalid code; use 1-40 letters, digits, or hyphens.")
    if not isinstance(run, str) or not RUN_PATTERN.fullmatch(run):
        raise ReconcileError("Invalid run; use r1, r2, and so on.")
    prefix = f"{code}-{run}"
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise ReconcileError("Combined event prefix must be at most 40 valid characters.")
    if len(code) + 4 > 32:
        raise ReconcileError("Code is too long for the event device_id limit.")
    return prefix


def load_gateway_result(path, expected_prefix):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReconcileError("Cannot read a valid .local/w06-last-run.json.") from None
    if not isinstance(data, dict) or data.get("prefix") != expected_prefix:
        raise ReconcileError("Gateway result prefix does not match this run.")
    count = data.get("puback_ok")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 32:
        raise ReconcileError("Gateway result has an invalid PUBACK count.")
    return count


def parse_consumer_counts(output):
    counts = {name: 0 for name in RESULT_NAMES}
    for line in output.splitlines():
        match = re.fullmatch(r"\s*(\d+)\s+result=(created|duplicate|rejected|error)\s*", line)
        if match:
            counts[match.group(2)] += int(match.group(1))
    return counts


def parse_remote_result(output):
    try:
        before, remaining = output.split("COUNTS_BEGIN\n", 1)
        counts_text, remaining = remaining.split("\nCOUNTS_END\n", 1)
    except ValueError:
        raise ReconcileError("Remote reconciliation returned an invalid consumer count response.") from None
    if before.strip():
        raise ReconcileError("Remote reconciliation returned unexpected output.")
    db_matches = re.findall(r"(?m)^DBCOUNT=(\d+)\s*$", remaining)
    if len(db_matches) != 1:
        raise ReconcileError("Remote reconciliation returned an invalid database count.")
    return parse_consumer_counts(counts_text), int(db_matches[0])


def validate_last_run_prefix(path, prefix):
    count = load_gateway_result(path, prefix)
    return count


def load_operator_token(path):
    env_path = Path(path)
    try:
        if env_path.is_symlink() or not env_path.is_file():
            raise ReconcileError(".local/app.env is missing or is not a regular file.")
        if stat.S_IMODE(env_path.stat().st_mode) != 0o600:
            raise ReconcileError(".local/app.env permissions must be exactly 600.")
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except ReconcileError:
        raise
    except (OSError, UnicodeError):
        raise ReconcileError("Cannot read .local/app.env.") from None
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key == "OPERATOR_TOKEN":
            if key in values:
                raise ReconcileError(".local/app.env contains duplicate operator settings.")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value or any(char.isspace() for char in value):
                raise ReconcileError(".local/app.env has an invalid operator setting.")
            values[key] = value
    if "OPERATOR_TOKEN" not in values:
        raise ReconcileError(".local/app.env has no operator token.")
    return values["OPERATOR_TOKEN"]


def fetch_json(url, token=None):
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            if response.status < 200 or response.status >= 300:
                raise ReconcileError(f"HTTP request failed with status {response.status}.")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ReconcileError(f"HTTP request failed with status {exc.code}.") from None
    except (URLError, TimeoutError, OSError):
        raise ReconcileError("HTTP request failed or timed out.") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ReconcileError("HTTP endpoint returned invalid JSON.") from None


def read_health(public_ip):
    health = fetch_json(f"http://{public_ip}/health")
    if not isinstance(health, dict) or not isinstance(health.get("version"), str):
        raise ReconcileError("Health endpoint response is missing a version.")
    if not isinstance(health.get("db_configured"), bool):
        raise ReconcileError("Health endpoint response has an invalid db_configured value.")
    return health


def read_filtered_events(public_ip, code, prefix, token):
    device_id = f"{code}-d01"
    query = urlencode({"device_id": device_id})
    data = fetch_json(f"http://{public_ip}/events?{query}", token)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ReconcileError("Filtered events endpoint returned an invalid response.")
    ids = []
    for event in data["events"]:
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id.startswith(prefix + "-"):
            ids.append(event_id)
    return ids


def resolve_target_and_ip():
    sys.path.insert(0, str(ROOT / "scripts"))
    from lab import LabError, context, run_aws, verify
    from resources import ResourceLedgerError, load_deployment_target

    try:
        target = load_deployment_target(RESOURCES_FILE)
        ctx = context()
        if ctx.get("region") != REGION:
            raise ReconcileError("Learner Lab context region must be us-east-1.")
        with redirect_stdout(io.StringIO()):
            verify(ctx)
        result = run_aws(
            [
                "ec2", "describe-instances",
                "--instance-ids", target["instance_id"],
                "--query",
                "Reservations[0].Instances[0].{State:State.Name,PublicIp:PublicIpAddress,KeyName:KeyName}",
            ],
            REGION,
        )
    except (
        LabError,
        ResourceLedgerError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        raise ReconcileError("Cannot verify the Learner Lab target or read the tracked EC2 instance.") from None
    if not isinstance(result, dict) or result.get("State") != "running":
        raise ReconcileError("Tracked EC2 instance is not running.")
    if result.get("KeyName") != target["key_name"]:
        raise ReconcileError("Tracked EC2 key pair does not match the live instance.")
    try:
        public_ip = str(ipaddress.IPv4Address(result["PublicIp"]))
    except (KeyError, ipaddress.AddressValueError, TypeError):
        raise ReconcileError("Tracked EC2 instance has no valid current public IPv4 address.") from None
    key_path = Path.home() / ".ssh" / target["key_name"]
    if key_path.is_symlink() or not key_path.is_file():
        raise ReconcileError("Matching SSH private key is missing or invalid.")
    if stat.S_IMODE(key_path.stat().st_mode) != 0o600:
        raise ReconcileError("Matching SSH private key permissions must be exactly 600.")
    return public_ip, key_path


def ssh_query(public_ip, key_path, prefix, timeout=SSH_TIMEOUT):
    command = [
        "ssh",
        "-i", str(key_path),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        f"{SSH_USER}@{public_ip}",
        f"sudo bash -s -- {shlex.quote(prefix)}",
    ]
    try:
        result = subprocess.run(
            command,
            input=REMOTE_QUERY.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ReconcileError("SSH reconciliation query timed out.") from None
    except OSError:
        raise ReconcileError("SSH reconciliation query could not be started.") from None
    if result.returncode:
        raise ReconcileError("SSH reconciliation query failed; check consumer and database service status.")
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeError:
        raise ReconcileError("SSH reconciliation returned invalid output.") from None
    return parse_remote_result(output)


def collect_remote_counts(public_ip, key_path, prefix, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + POLL_TIMEOUT
    counts = {name: 0 for name in RESULT_NAMES}
    db_count = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return counts, db_count
        counts, db_count = ssh_query(
            public_ip,
            key_path,
            prefix,
            timeout=min(SSH_TIMEOUT, remaining),
        )
        if sum(counts.values()) >= 32 or clock() >= deadline:
            return counts, db_count
        sleep(min(POLL_INTERVAL, max(0, deadline - clock())))


def print_report(health, prefix, puback_count, counts, db_count, d01_event_ids):
    print(f"version={health['version']}")
    print(f"db_configured={str(health['db_configured']).lower()}")
    print(f"prefix={prefix}")
    print(f"puback_ok={puback_count}/32")
    print(
        "consumer created={created} duplicate={duplicate} rejected={rejected} error={error}".format(
            **counts
        )
    )
    print(f"database_count={db_count}")
    for event_id in d01_event_ids:
        print(f"event_id={event_id}")
    print(f"d01_count={len(d01_event_ids)}")


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        prefix = build_prefix(args.code, args.run)
        puback_count = validate_last_run_prefix(RUN_FILE, prefix)
        public_ip, key_path = resolve_target_and_ip()
        health = read_health(public_ip)
        operator_token = load_operator_token(APP_ENV_FILE)
        counts, db_count = collect_remote_counts(public_ip, key_path, prefix)
        event_ids = read_filtered_events(public_ip, args.code, prefix, operator_token)
        print_report(health, prefix, puback_count, counts, db_count, event_ids)
    except ReconcileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
