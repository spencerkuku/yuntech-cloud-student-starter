#!/usr/bin/env python3
"""Run the five W5 idempotency checks without printing secrets."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[4]
W5 = ROOT / "practice/spencerku/w5"


def read_env(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def call(base_url, method, path, body=None, token=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())
    except (URLError, TimeoutError) as error:
        return 0, {"error": type(error).__name__}


def host_details(instance_id):
    subprocess.run(["bash", str(ROOT / "scripts/verify-aws.sh")], cwd=ROOT, check=True)
    resources = json.loads((ROOT / "practice/spencerku/w3/.local/resources.json").read_text())
    if resources.get("instance_id") != instance_id or resources.get("owner") != "spencerku":
        raise SystemExit("STOP: instance does not match the recorded W3 resource owner")
    sys.path.insert(0, str(ROOT / "scripts"))
    import lab

    context = lab.context()
    result = lab.run_aws(["ec2", "describe-instances", "--instance-ids", instance_id], context["region"])
    instances = result.get("Reservations", [{}])[0].get("Instances", [])
    if len(instances) != 1 or instances[0].get("State", {}).get("Name") != "running":
        raise SystemExit("STOP: recorded W3 instance is not running")
    public_ip = instances[0].get("PublicIpAddress")
    if not public_ip:
        raise SystemExit("STOP: recorded W3 instance has no public IP")
    return "http://" + public_ip, resources, context["region"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id")
    parser.add_argument("--env-file", type=Path, default=W5 / ".local/app.env")
    parser.add_argument("--config-file", type=Path, default=W5 / ".local/w5.env")
    args = parser.parse_args()
    resources = json.loads((ROOT / "practice/spencerku/w3/.local/resources.json").read_text())
    instance_id = args.instance_id or resources.get("instance_id")
    base_url, recorded, region = host_details(instance_id)
    values = read_env(args.env_file)
    reporter = values.get("REPORTER_TOKEN", "")
    operator = values.get("OPERATOR_TOKEN", "")
    config = read_env(args.config_file)
    event = {
        "event_id": "g02-spencerku-w5-0001",
        "device_id": "g02-d01",
        "observed_at": "2026-10-06T09:00:00+08:00",
        "type": "test",
        "note": "w5-idempotency",
    }

    status, health = call(base_url, "GET", "/health")
    if status != 200:
        raise SystemExit(f"STOP: health returned HTTP {status}")
    print("version=" + str(health.get("version", "unknown")))
    print("db_configured=" + str(health.get("db_configured", False)).lower())

    failed = False

    def run_case(number, label, method, path, body, token, expected):
        nonlocal failed
        status, response = call(base_url, method, path, body, token)
        print(json.dumps({"case": number, "name": label, "status": status, "body": response},
                         ensure_ascii=False, sort_keys=True))
        failed = failed or status != expected

    run_case(1, "new event", "POST", "/events", event, reporter, 201)
    run_case(2, "same event again", "POST", "/events", event, reporter, 200)
    run_case(3, "same id with changed note", "POST", "/events",
             dict(event, note="changed"), reporter, 409)

    ssh_key = Path(config["SSH_KEY_FILE"].replace("~", str(Path.home())))
    ssh = ["ssh", "-i", str(ssh_key), "-o", "BatchMode=yes", "-o",
           "ConnectTimeout=10", f"{config['SSH_USER']}@{base_url.removeprefix('http://')}"]
    restart = subprocess.run(ssh + ["sudo", "systemctl", "restart", "inspection"],
                             capture_output=True, text=True)
    if restart.returncode:
        raise SystemExit("STOP: could not restart inspection on EC2")
    run_case(4, "event survives service restart", "GET",
             "/events/" + event["event_id"], None, operator, 200)

    remote_sql = (
        "sudo bash -c 'set -a; . /etc/inspection/app.env; set +a; "
        'PGPASSWORD=\"$DB_PASSWORD\" psql '
        '"host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full '
        'sslrootcert=/etc/inspection/rds-ca.pem" -At '
        '-v event_id="' + event["event_id"] + '" '
        "\"-c\" \"SELECT count(*) FROM events WHERE event_id = :'event_id';\"'"
    )
    count = subprocess.run(ssh + [remote_sql], capture_output=True, text=True)
    if count.returncode:
        raise SystemExit("STOP: EC2 psql query failed")
    print(json.dumps({"case": 5, "name": "database count", "status": 200,
                      "body": {"count": count.stdout.strip()}}, ensure_ascii=False, sort_keys=True))
    failed = failed or count.stdout.strip() != "1"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
