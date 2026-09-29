#!/usr/bin/env python3
"""W4 T3 rejection matrix: all seven rows in one run, against the live host.

Reads both tokens from .local/app.env (600) and never prints them, never passes them
as command-line arguments and never writes them to a log. Re-queries the host's CURRENT
public IPv4 through the control plane first, so it never reuses a stale address.

  python3 tests/reject_matrix.py            # uses the recorded host in .local/resources.json
  python3 tests/reject_matrix.py --ip 1.2.3.4

Exit code 0 only when every row matches the expected status.
"""
import argparse
import http.client
import json
from pathlib import Path
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SECRET = ROOT / ".local" / "app.env"
RESOURCES = ROOT / ".local" / "resources.json"
VALID = ROOT / "tests" / "fixtures" / "event_01_valid.json"
BAD_TZ = ROOT / "tests" / "fixtures" / "event_02_bad_observed_at.json"
JSON_CT = "application/json"


def die(message):
    print(f"STOP: {message}", file=sys.stderr)
    raise SystemExit(2)


def load_tokens():
    if not SECRET.is_file():
        die(f"{SECRET} missing. Create it with umask 077 and mode 600.")
    mode = oct(SECRET.stat().st_mode & 0o777)[2:]
    if mode != "600":
        die(f"{SECRET} mode is {mode}, not 600.")
    values = {}
    for line in SECRET.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    for key in ("REPORTER_TOKEN", "OPERATOR_TOKEN"):
        if not values.get(key):
            die(f"{SECRET} has no {key}.")
    return values["REPORTER_TOKEN"], values["OPERATOR_TOKEN"]


def current_ip():
    """Control plane: never reuse the address recorded in a previous week."""
    from lab import run_aws, context, LabError
    rounds = json.loads(RESOURCES.read_text(encoding="utf-8"))["rounds"]
    live = [r for r in rounds if r.get("instance", {}).get("state") != "terminated"]
    if not live:
        die("no live round in .local/resources.json")
    iid = live[-1]["instance"]["id"]
    try:
        row = run_aws(["ec2", "describe-instances", "--instance-ids", iid,
                       "--query", "Reservations[0].Instances[0].{S:State.Name,P:PublicIpAddress}"],
                      context()["region"])
    except (LabError, TypeError) as exc:
        die(f"could not read the host's current address: {exc}")
    if row.get("S") != "running" or not row.get("P"):
        die(f"instance {iid} is {row.get('S')} without a public IPv4; start it first")
    return iid, row["P"]


def request(host, method, path, token=None, body=None, content_type=JSON_CT):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if content_type:
            headers["Content-Type"] = content_type
    connection = http.client.HTTPConnection(host, 80, timeout=10)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8", "replace")
    finally:
        connection.close()
    try:
        return response.status, json.loads(text)
    except json.JSONDecodeError:
        return response.status, text


def main():
    parser = argparse.ArgumentParser(description="W4 rejection matrix")
    parser.add_argument("--ip", help="host IPv4; default is the live address from AWS")
    args = parser.parse_args()

    reporter, operator = load_tokens()
    if args.ip:
        host, iid = args.ip, "(given by --ip)"
    else:
        iid, host = current_ip()

    status, health = request(host, "GET", "/health")
    if status != 200 or not isinstance(health, dict):
        die(f"/health returned {status}; is the service up at {host}?")
    print(f"host: {host}  instance: {iid}")
    print(f"version: {health.get('version')}  auth_configured: {health.get('auth_configured')}")
    print(f"started_at: {health.get('started_at')}")
    if health.get("auth_configured") is not True:
        die("auth_configured is not true; deploy.sh did not install the secret file")

    # #1 and #5 need an event_id that is not already on the host, so derive a fresh one
    # from the fixture's own group/owner prefix rather than inventing classroom data.
    template = json.loads(VALID.read_text(encoding="utf-8"))
    event = dict(template)
    event["event_id"] = template["event_id"]
    listed_before = request(host, "GET", "/events", token=operator)[1]
    already = {item.get("event_id") for item in listed_before} if isinstance(listed_before, list) else set()
    attempt = 1
    while event["event_id"] in already:
        attempt += 1
        stem, _, serial = template["event_id"].rpartition("-")
        if not stem:
            die(f"fixture event_id {template['event_id']!r} has no <stem>-<serial> shape")
        event["event_id"] = f"{stem}-{attempt:04d}"
    print(f"using event_id: {event['event_id']} (not already on the host)")
    print()

    # (number, what was sent, method, path, token, body, expected status)
    rows = [
        ("1", "reporter sends a valid event", "POST", "/events", reporter, event, 201),
        ("2", "same request without a token", "POST", "/events", None, event, 401),
        ("3", "operator token sends an event", "POST", "/events", operator, event, 403),
        ("4", "reporter, observed_at has no timezone", "POST", "/events", reporter,
         json.loads(BAD_TZ.read_text(encoding="utf-8")), 400),
        ("5", "reporter sends #1 again", "POST", "/events", reporter, event, 409),
        ("6", "reporter token reads the list", "GET", "/events", reporter, None, 403),
        ("7", "operator token reads the list", "GET", "/events", operator, None, 200),
    ]

    print(f"{'#':<3}{'request':<40}{'expect':<8}{'got':<6}{'match':<7}response")
    print("-" * 110)
    failures = []
    seen_created = None
    for number, title, method, path, token, body, expected in rows:
        # Content-Type is always application/json here; rows 1-5 differ only in token/body,
        # so any 400 about content_type would mean this script is broken, not the service.
        status, response = request(host, method, path, token=token, body=body,
                                   content_type=JSON_CT)
        if isinstance(response, dict) and response.get("field") == "content_type":
            die(f"row #{number} was rejected on Content-Type: this script is at fault, "
                "not the service")
        ok = status == expected
        if not ok:
            failures.append((number, title, expected, status, response))
        if number == "1" and ok:
            seen_created = response
        text = json.dumps(response, ensure_ascii=False) if not isinstance(response, str) else response
        if len(text) > 160:
            text = text[:157] + "..."
        print(f"{number:<3}{title:<40}{expected:<8}{status:<6}{'OK' if ok else 'MISMATCH':<7}{text}")

    print()
    if failures:
        print(f"MISMATCHES: {len(failures)} row(s) did not match the contract:")
        for number, title, expected, status, response in failures:
            print(f"  #{number} {title}: expected {expected}, got {status} -> {response}")
        return 1

    if isinstance(seen_created, dict) and "event_id" in seen_created:
        print(f"#1 created: event_id={seen_created['event_id']} "
              f"received_at={seen_created.get('received_at')}")
    if isinstance(listed_before, list) or True:
        final_status, listed = request(host, "GET", "/events", token=operator)
        if isinstance(listed, list) and isinstance(seen_created, dict):
            match = [item for item in listed
                     if item.get("event_id") == seen_created.get("event_id")]
            print(f"#7 list contains #1: "
                  f"{'yes' if match else 'NO'}  (received_at matches: "
                  f"{'yes' if match and match[0].get('received_at') == seen_created.get('received_at') else 'NO'})")
    print(f"all 7 rows matched; list holds {len(listed) if isinstance(listed, list) else '?'} event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
