#!/usr/bin/env python3
"""Run the live W5 event idempotency matrix without printing credentials."""
import argparse
import datetime
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class MatrixError(Exception):
    pass


def read_token_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise MatrixError(f"{path.name} must be a regular file with mode 600.")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    if not values.get("REPORTER_TOKEN") or not values.get("OPERATOR_TOKEN"):
        raise MatrixError("Both application tokens must be present in app.env.")
    return values


def http_request(url, token=None, method="GET", body=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MatrixError(f"HTTP request failed ({type(exc).__name__}); credentials were not printed.") from exc
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        decoded = {"body": payload.decode("utf-8", errors="replace")}
    return status, decoded


def ssh(key, user, host, command, stdin=None, timeout=60):
    try:
        result = subprocess.run(
            [
                "ssh", "-i", str(key), "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10", f"{user}@{host}", command,
            ],
            input=stdin, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MatrixError(f"SSH step failed ({type(exc).__name__}); remote output was suppressed.") from exc
    if result.returncode != 0:
        raise MatrixError("SSH step failed; remote diagnostics were suppressed to protect connection settings.")
    return result.stdout.strip()


def redact_body(value, known_secrets):
    if isinstance(value, dict):
        return {
            key: "[redacted]" if re.search(r"token|password|secret|connection|string", key, re.I)
            else redact_body(item, known_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_body(item, known_secrets) for item in value]
    if isinstance(value, str):
        for secret in known_secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)postgres(?:ql)?://[^\s\"']+", "[redacted connection string]", value)
        value = re.sub(
            r"(?i)(?:host|dbname)=[^\s\"']+(?:\s+[A-Za-z_][A-Za-z0-9_]*=[^\s\"']+)+",
            "[redacted connection string]",
            value,
        )
        value = re.sub(r"(?i)\b(?:password|token|secret)=[^\s\"']+", "[redacted]", value)
    return value


def display_row(number, status, body, known_secrets):
    safe_body = redact_body(body, known_secrets)
    print(f"#{number} HTTP {status} {json.dumps(safe_body, ensure_ascii=False, separators=(',', ':'))}")


def run(args):
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.host):
        raise MatrixError("Host must be an IPv4 address or DNS name.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", args.user):
        raise MatrixError("Invalid SSH user.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.event_id):
        raise MatrixError("event_id must match the public event contract.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.device_id):
        raise MatrixError("device_id must match the public event contract.")
    key = Path(args.key).expanduser()
    if key.is_symlink() or not key.is_file() or stat.S_IMODE(key.stat().st_mode) != 0o600:
        raise MatrixError("SSH private key must be a regular file with mode 600.")
    key = key.resolve()
    tokens = read_token_file(ROOT / ".local" / "app.env")
    base = f"http://{args.host}"
    status, health = http_request(base + "/health")
    if status != 200 or not isinstance(health, dict) or health.get("db_configured") is not True:
        raise MatrixError("Host health check failed or db_configured is not true.")
    print(f"version={health.get('version')} db_configured={str(health.get('db_configured')).lower()}")

    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    original = {
        "event_id": args.event_id,
        "device_id": args.device_id,
        "observed_at": observed_at,
        "type": "status",
        "note": "W5 idempotency matrix",
    }
    first_status, first_body = http_request(base + "/events", tokens["REPORTER_TOKEN"], "POST", original)
    known_secrets = tokens.values()
    display_row(1, first_status, first_body, known_secrets)
    second_status, second_body = http_request(base + "/events", tokens["REPORTER_TOKEN"], "POST", original)
    display_row(2, second_status, second_body, known_secrets)
    changed = dict(original)
    changed["note"] = "W5 idempotency conflict check"
    third_status, third_body = http_request(base + "/events", tokens["REPORTER_TOKEN"], "POST", changed)
    display_row(3, third_status, third_body, known_secrets)

    restart = "sudo systemctl restart inspection"
    ssh(key, args.user, args.host, restart)
    fourth_status, fourth_body = http_request(
        base + "/events/" + args.event_id, tokens["OPERATOR_TOKEN"],
    )
    display_row(4, fourth_status, fourth_body, known_secrets)
    count_script = (
        "set -euo pipefail\n"
        "set -a\n"
        ". /etc/inspection/app.env\n"
        "set +a\n"
        "PGPASSWORD=\"$DB_PASSWORD\" psql "
        "\"host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full "
        "sslrootcert=/etc/inspection/rds-ca.pem\" "
        f"-v event_id=\"{args.event_id}\" -At <<'W5_SQL'\n"
        "SELECT count(*) FROM events WHERE event_id = :'event_id';\n"
        "W5_SQL\n"
    )
    count_text = ssh(key, args.user, args.host, "sudo bash -s", stdin=count_script, timeout=60)
    if not re.fullmatch(r"\d+", count_text):
        raise MatrixError("psql did not return a single row count.")
    count = int(count_text)
    print(f"#5 EC2 psql count={count}")

    checks = [
        (first_status == 201, "new event status"),
        (second_status == 200, "duplicate status"),
        (first_body.get("received_at") == second_body.get("received_at"), "duplicate received_at"),
        (third_status == 409, "conflicting duplicate status"),
        (fourth_status == 200, "post-restart GET status"),
        (fourth_body.get("event_id") == args.event_id, "post-restart event id"),
        (fourth_body.get("received_at") == first_body.get("received_at"), "post-restart received_at"),
        (count == 1, "database row count"),
    ]
    failed = [name for passed, name in checks if not passed]
    if failed:
        raise MatrixError("Matrix checks failed: " + ", ".join(failed))
    print("Matrix checks: PASS.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--user", default="ec2-user")
    args = parser.parse_args()
    try:
        run(args)
    except (MatrixError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, MatrixError) else type(exc).__name__
        print("STOP: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
