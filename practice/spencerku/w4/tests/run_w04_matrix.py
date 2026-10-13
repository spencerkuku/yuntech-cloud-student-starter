#!/usr/bin/env python3
"""Run the seven W4 rejection-matrix requests without printing tokens."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = Path(__file__).with_name("fixtures")


def read_env(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def call(base_url, method, path, body=None, token=None):
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())
    except (URLError, TimeoutError) as error:
        return 0, {"error": str(error)}


def current_base_url(instance_id):
    subprocess.run(["bash", str(ROOT / "scripts/verify-aws.sh")], cwd=ROOT, check=True)
    resources = json.loads((ROOT / "practice/spencerku/w3/.local/resources.json").read_text(encoding="utf-8"))
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
        raise SystemExit("STOP: recorded W3 instance has no current public IP")
    return "http://" + public_ip


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", default=None)
    parser.add_argument("--env-file", type=Path, default=ROOT / "practice/spencerku/w4/.local/app.env")
    args = parser.parse_args()
    resources = json.loads((ROOT / "practice/spencerku/w3/.local/resources.json").read_text(encoding="utf-8"))
    instance_id = args.instance_id or resources.get("instance_id")
    if not instance_id:
        raise SystemExit("STOP: missing recorded W3 instance ID")
    base_url = current_base_url(instance_id)
    values = read_env(args.env_file)
    reporter = values.get("REPORTER_TOKEN", "")
    operator = values.get("OPERATOR_TOKEN", "")
    event = json.loads((FIXTURES / "valid-event.json").read_text(encoding="utf-8"))
    health_status, health = call(base_url, "GET", "/health")
    if health_status != 200:
        print(f"health failed: HTTP {health_status}")
        return 1
    print("version=" + str(health.get("version", "unknown")))
    cases = [
        (1, "reporter legal event", "POST", "/events", event, reporter, 201),
        (2, "no token", "POST", "/events", dict(event, event_id="g02-no-token"), None, 401),
        (3, "operator sends event", "POST", "/events", dict(event, event_id="g02-operator"), operator, 403),
        (4, "observed_at without timezone", "POST", "/events", json.loads((FIXTURES / "invalid-event-no-timezone.json").read_text(encoding="utf-8")), reporter, 400),
        (5, "duplicate event", "POST", "/events", event, reporter, 409),
        (6, "reporter reads list", "GET", "/events", None, reporter, 403),
        (7, "operator reads list", "GET", "/events", None, operator, 200),
    ]
    failed = False
    for number, label, method, path, body, token, expected in cases:
        status, response = call(base_url, method, path, body, token)
        print(json.dumps({"case": number, "name": label, "status": status, "body": response}, ensure_ascii=False, sort_keys=True))
        if status != expected:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
