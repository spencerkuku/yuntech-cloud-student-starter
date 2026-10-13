#!/usr/bin/env bash
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p .local/iot
chmod 700 .local/iot

python3 - <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "scripts"))
from lab import context, run_aws, verify

ctx = verify(context())
region = ctx["region"]
resources_path = ROOT / ".local" / "resources.json"
resources = json.loads(resources_path.read_text(encoding="utf-8"))
resources.setdefault("iot_gw_cert_id", None)
resources.setdefault("iot_consumer_cert_id", None)
resources.setdefault("iot_gw_policy", None)
resources.setdefault("iot_consumer_policy", None)
config = {}
for line in (ROOT / ".local" / "config").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
code = config.get("OWNER", "")
if not code or not re.fullmatch(r"[A-Za-z0-9-]{1,40}", code):
    raise SystemExit("STOP: OWNER must match ^[A-Za-z0-9-]{1,40}$")
iot = ROOT / ".local" / "iot"

def save():
    temporary = resources_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(resources, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, resources_path)

endpoint = iot / "endpoint"
if not endpoint.exists():
    value = run_aws(["iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS"], region)["endpointAddress"]
    endpoint.write_text(value + "\n", encoding="utf-8")
    os.chmod(endpoint, 0o600)

def create_cert(resource_key, cert_name, key_name):
    if resources[resource_key]:
        return
    result = run_aws(["iot", "create-keys-and-certificate", "--set-as-active"], region)
    cert = iot / cert_name
    key = iot / key_name
    cert.write_text(result["certificatePem"], encoding="utf-8")
    key.write_text(result["keyPair"]["PrivateKey"], encoding="utf-8")
    os.chmod(cert, 0o600)
    os.chmod(key, 0o600)
    resources[resource_key] = result["certificateId"]
    save()

create_cert("iot_gw_cert_id", "gw.cert.pem", "gw.private.key")
create_cert("iot_consumer_cert_id", "consumer.cert.pem", "consumer.private.key")

account = ctx["account"]
topic = f"cxe/{code}/gw/events"
gw_policy = f"cxe-{code}-gw"
consumer_policy = f"cxe-{code}-consumer"
gw_doc = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"arn:aws:iot:{region}:{account}:client/gw-{code}"},
    {"Effect": "Allow", "Action": "iot:Publish", "Resource": f"arn:aws:iot:{region}:{account}:topic/{topic}"},
]}, separators=(",", ":"))
consumer_doc = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"arn:aws:iot:{region}:{account}:client/consumer-{code}"},
    {"Effect": "Allow", "Action": "iot:Subscribe", "Resource": f"arn:aws:iot:{region}:{account}:topicfilter/{topic}"},
    {"Effect": "Allow", "Action": "iot:Receive", "Resource": f"arn:aws:iot:{region}:{account}:topic/{topic}"},
]}, separators=(",", ":"))

for key, name, document in (("iot_gw_policy", gw_policy, gw_doc), ("iot_consumer_policy", consumer_policy, consumer_doc)):
    if not resources[key]:
        run_aws(["iot", "create-policy", "--policy-name", name, "--policy-document", document], region)
        resources[key] = name
        save()

for cert_key, policy_name in (("iot_gw_cert_id", gw_policy), ("iot_consumer_cert_id", consumer_policy)):
    run_aws(["iot", "attach-policy", "--policy-name", policy_name,
             "--target", f"arn:aws:iot:{region}:{account}:cert/{resources[cert_key]}"], region)

ca = iot / "AmazonRootCA1.pem"
if not ca.exists():
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                    "https://www.amazontrust.com/repository/AmazonRootCA1.pem", "-o", str(ca)], check=True)
    os.chmod(ca, 0o600)
for name in ("gw.cert.pem", "gw.private.key", "consumer.cert.pem", "consumer.private.key", "AmazonRootCA1.pem", "endpoint"):
    os.chmod(iot / name, 0o600)
for cert_key in ("iot_gw_cert_id", "iot_consumer_cert_id"):
    status = run_aws(["iot", "describe-certificate", "--certificate-id", resources[cert_key]], region)
    if status["certificateDescription"]["status"] != "ACTIVE":
        raise SystemExit("STOP: IoT certificate is not ACTIVE")
print("IoT identities ready; certificates ACTIVE")
PY