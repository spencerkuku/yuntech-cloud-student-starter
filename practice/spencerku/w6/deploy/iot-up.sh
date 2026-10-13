#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
LOCAL="$ROOT/practice/spencerku/w6/.local"
RESOURCES="$LOCAL/resources.json"
mkdir -p "$LOCAL/iot"
chmod 700 "$LOCAL/iot"
IOT_APPROVED="${IOT_APPROVED:-0}" python3 - "$ROOT" "$LOCAL" "$RESOURCES" <<'PY'
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

root, local, resources_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "scripts"))
import lab

ctx = lab.verify()
data = json.loads(resources_path.read_text(encoding="utf-8")) if resources_path.exists() else {}
code = "g03-m2"
endpoint = lab.run_aws(["iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS"], ctx["region"], raw=True)
iot = local / "iot"
iot.joinpath("endpoint").write_text(endpoint.strip() + "\n", encoding="utf-8")
iot.joinpath("endpoint").chmod(0o600)
account = ctx["account"]
base = f"arn:aws:iot:{ctx['region']}:{account}"
print(
    f"IoT create: code={code}, region={ctx['region']}, resources=2 certificates, "
    f"2 exact client/topic policies, Root CA; no IAM changes; recovery=retain IoT resources"
)
if os.environ.get("IOT_APPROVED") != "1":
    raise SystemExit("STOP: review scope and rerun with IOT_APPROVED=1")

def save():
    lab.atomic_write(resources_path, json.dumps(data, indent=2) + "\n")

def create_cert(role):
    cert_path = iot / f"{role}.cert.pem"
    key_path = iot / f"{role}.private.key"
    key_id = f"iot_{role}_cert_id"
    if data.get(key_id):
        return
    with tempfile.NamedTemporaryFile("w+b", dir=iot, prefix=".cert-", delete=False) as stream:
        output = Path(stream.name)
    try:
        command = ["aws", "--profile", lab.PROFILE, "--region", ctx["region"],
                   "--no-cli-pager", "iot", "create-keys-and-certificate",
                   "--set-as-active", "--output", "json"]
        result = subprocess.run(command, env=lab.clean_env(ctx["region"]),
                                stdout=output.open("w"), stderr=subprocess.PIPE,
                                text=True, check=False)
        if result.returncode:
            raise lab.LabError("iot create-keys-and-certificate: CommandFailed")
        payload = json.loads(output.read_text(encoding="utf-8"))
        cert_path.write_text(payload["certificatePem"], encoding="utf-8")
        key_path.write_text(payload["keyPair"]["PrivateKey"], encoding="utf-8")
        cert_path.chmod(0o600); key_path.chmod(0o600)
        data[key_id] = payload["certificateId"]
        save()
    finally:
        output.unlink(missing_ok=True)

create_cert("gw")
create_cert("consumer")
ca = iot / "AmazonRootCA1.pem"
if not ca.exists():
    import urllib.request
    ca.write_bytes(urllib.request.urlopen(
        "https://www.amazontrust.com/repository/AmazonRootCA1.pem", timeout=15
    ).read())
    ca.chmod(0o600)

policies = {
    "iot_gw_policy": ("cxe-" + code + "-gw", {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"{base}:client/gw-{code}"},
            {"Effect": "Allow", "Action": "iot:Publish", "Resource": f"{base}:topic/cxe/{code}/gw/events"},
        ],
    }),
    "iot_consumer_policy": ("cxe-" + code + "-consumer", {
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect", "Resource": f"{base}:client/consumer-{code}"},
            {"Effect": "Allow", "Action": "iot:Subscribe", "Resource": f"{base}:topicfilter/cxe/{code}/gw/events"},
            {"Effect": "Allow", "Action": "iot:Receive", "Resource": f"{base}:topic/cxe/{code}/gw/events"},
        ],
    }),
}
for key, (name, document) in policies.items():
    if not data.get(key):
        result = lab.run_aws(["iot", "create-policy", "--policy-name", name,
                              "--policy-document", json.dumps(document)], ctx["region"])
        data[key] = name
        save()
    certificate_id = data["iot_gw_cert_id" if key == "iot_gw_policy" else "iot_consumer_cert_id"]
    certificate_arn = f"{base}:cert/{certificate_id}"
    lab.run_aws(["iot", "attach-policy", "--policy-name", name,
                 "--target", certificate_arn], ctx["region"])
data["week"] = "w06"; data["status"] = "iot-ready"; save()
for role in ("gw", "consumer"):
    status = lab.run_aws(
        ["iot", "describe-certificate", "--certificate-id",
         data[f"iot_{role}_cert_id"], "--query", "certificateDescription.status"],
        ctx["region"], raw=True,
    ).strip().strip('"')
    if status != "ACTIVE":
        raise SystemExit("STOP: IoT certificate is not ACTIVE")
print("IoT resources ready; certificates ACTIVE")
PY
