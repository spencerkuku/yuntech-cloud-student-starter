#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${ROOT_DIR}/.local"
RESOURCES_PATH="${LOCAL_DIR}/resources.json"
APP_ENV_PATH="${LOCAL_DIR}/app.env"
USER_DATA_PATH="${LOCAL_DIR}/w04-user-data.sh"
COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"

usage() {
  printf 'Usage: bash deploy/deploy.sh\n'
}

if [[ $# -gt 0 ]]; then
  if [[ $# -eq 1 && "$1" == "--help" ]]; then
    usage
    exit 0
  fi
  usage >&2
  exit 2
fi

if [[ ! -f "$APP_ENV_PATH" || -L "$APP_ENV_PATH" ]]; then
  echo 'STOP: .local/app.env must exist as a regular, non-symlink file.' >&2
  exit 1
fi
python3 - "$APP_ENV_PATH" <<'PY'
import re
import stat
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
mode = stat.S_IMODE(env_path.stat().st_mode)
if mode != 0o600:
    raise SystemExit("STOP: .local/app.env permissions must be exactly 600.")
values = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
  line = line.strip()
  if not line or line.startswith("#"):
    continue
  key, separator, value = line.partition("=")
  if not separator or key not in {"REPORTER_TOKEN", "OPERATOR_TOKEN"} or key in values:
    raise SystemExit("STOP: .local/app.env must contain the two W4 token settings.")
  value = value.strip()
  if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
    value = value[1:-1]
  if not value or re.search(r"\s", value):
    raise SystemExit("STOP: .local/app.env contains an empty or invalid token setting.")
  values[key] = value
if set(values) != {"REPORTER_TOKEN", "OPERATOR_TOKEN"}:
  raise SystemExit("STOP: .local/app.env must contain the two W4 token settings.")
PY

if [[ ! -f "$RESOURCES_PATH" || -L "$RESOURCES_PATH" ]]; then
  echo 'STOP: .local/resources.json is missing or not a regular file.' >&2
  exit 1
fi

# The package builder reads committed app/service.py and deploy/nginx.conf only.
for source in app/service.py deploy/nginx.conf deploy/make_user_data.py deploy/make-user-data.sh deploy/deploy.sh; do
  if ! git -C "$ROOT_DIR" ls-files --error-unmatch "$source" >/dev/null 2>&1; then
    echo "STOP: deployment source is not tracked: ${source}" >&2
    exit 1
  fi
  if ! git -C "$ROOT_DIR" diff --quiet HEAD -- "$source"; then
    echo "STOP: uncommitted deployment source change: ${source}" >&2
    exit 1
  fi
done

if ! git -C "$ROOT_DIR" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
  echo 'STOP: HEAD is not a committed version.' >&2
  exit 1
fi

bash "$ROOT_DIR/deploy/make-user-data.sh" "$COMMIT" "$USER_DATA_PATH"
python3 - "$APP_ENV_PATH" "$USER_DATA_PATH" <<'PY'
import sys
from pathlib import Path

env_path, user_data_path = map(Path, sys.argv[1:])
secrets = []
for line in env_path.read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and key in {"REPORTER_TOKEN", "OPERATOR_TOKEN"} and value:
        secrets.append(value.encode("utf-8"))
payload = user_data_path.read_bytes()
if any(secret in payload for secret in secrets):
    raise SystemExit("STOP: app.env secret detected in generated user data.")
PY

bash "$ROOT_DIR/scripts/verify-aws.sh"

PLAN="$(python3 - "$ROOT_DIR" "$RESOURCES_PATH" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

root, resources_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from scripts.lab import context, run_aws

ctx = context()
identity = run_aws(["sts", "get-caller-identity"], ctx["region"])
if identity.get("Account") != ctx["account"] or ":assumed-role/" not in identity.get("Arn", ""):
    raise SystemExit("STOP: learnerlab identity does not match its saved context.")
try:
    resources = json.loads(resources_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit("STOP: cannot read .local/resources.json.")
instance_id = resources.get("instance_id")
key_name = resources.get("key_name")
if not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]+", instance_id):
    raise SystemExit("STOP: resource ledger has no valid instance_id.")
if not isinstance(key_name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", key_name):
    raise SystemExit("STOP: resource ledger has no valid key_name.")
response = run_aws([
    "ec2", "describe-instances", "--instance-ids", instance_id,
    "--query", "Reservations[0].Instances[0].{State:State.Name,PublicIp:PublicIpAddress,KeyName:KeyName,Tags:Tags}"
], ctx["region"])
if not response or response.get("KeyName") != key_name:
    raise SystemExit("STOP: instance does not match the tracked key pair.")
tags = {item.get("Key"): item.get("Value") for item in response.get("Tags", [])}
if tags.get("course") != "yuntech-115-1" or tags.get("week") != "w03":
    raise SystemExit("STOP: tracked instance is missing expected course ownership tags.")
state = response.get("State")
if state not in {"running", "stopped"}:
    raise SystemExit(f"STOP: tracked instance is in unsupported state: {state}.")
if state == "running" and not response.get("PublicIp"):
    raise SystemExit("STOP: running instance has no public IPv4 address.")
print(json.dumps({
    "region": ctx["region"],
    "instance_id": instance_id,
    "key_name": key_name,
    "state": state,
    "public_ip": response.get("PublicIp"),
}))
PY
)"
REGION="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["region"])' <<<"$PLAN")"
INSTANCE_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["instance_id"])' <<<"$PLAN")"
KEY_NAME="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["key_name"])' <<<"$PLAN")"
INSTANCE_STATE="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])' <<<"$PLAN")"
CURRENT_IP="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["public_ip"] or "")' <<<"$PLAN")"

KEY_PATH="${HOME}/.ssh/${KEY_NAME}"
if [[ ! -f "$KEY_PATH" || -L "$KEY_PATH" ]]; then
  echo 'STOP: matching SSH private key is missing or not a regular file.' >&2
  exit 1
fi
python3 - "$KEY_PATH" <<'PY'
import stat
import sys
from pathlib import Path

if stat.S_IMODE(Path(sys.argv[1]).stat().st_mode) != 0o600:
    raise SystemExit("STOP: SSH private key permissions must be exactly 600.")
PY

printf 'W4 deployment target\n'
printf 'Region: %s\n' "$REGION"
printf 'Instance: %s\n' "$INSTANCE_ID"
if [[ "$INSTANCE_STATE" == "running" ]]; then
  printf 'Current public IP (will be refreshed before SSH): %s\n' "$CURRENT_IP"
else
  printf 'Current public IP: assigned after starting the stopped instance\n'
fi
printf 'Commit: %s\n' "$COMMIT"
if [[ ! -t 0 ]]; then
  echo 'STOP: interactive confirmation is required; no deployment started.' >&2
  exit 1
fi
read -r -p "Type 'DEPLOY W4' to continue: " approval
if [[ "$approval" != 'DEPLOY W4' ]]; then
  echo 'Cancelled; no deployment started.' >&2
  exit 1
fi

PUBLIC_IP="$(python3 - "$ROOT_DIR" "$RESOURCES_PATH" <<'PY'
import json
import re
import sys
from pathlib import Path

root, resources_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from scripts.lab import context, run_aws

ctx = context()
resources = json.loads(resources_path.read_text(encoding="utf-8"))
instance_id = resources["instance_id"]
response = run_aws([
    "ec2", "describe-instances", "--instance-ids", instance_id,
    "--query", "Reservations[0].Instances[0].{State:State.Name,KeyName:KeyName}"
], ctx["region"])
if response.get("KeyName") != resources.get("key_name"):
    raise SystemExit("STOP: tracked instance key pair changed.")
state = response.get("State")
if state == "stopped":
    run_aws(["ec2", "start-instances", "--instance-ids", instance_id], ctx["region"])
elif state != "running":
    raise SystemExit(f"STOP: tracked instance is in unsupported state: {state}.")
deadline = time.monotonic() + 300
while True:
  refreshed = run_aws([
    "ec2", "describe-instances", "--instance-ids", instance_id,
    "--query", "Reservations[0].Instances[0].{State:State.Name,PublicIp:PublicIpAddress,KeyName:KeyName}"
  ], ctx["region"])
  if refreshed.get("State") == "running":
    break
  if refreshed.get("State") != "pending" or time.monotonic() >= deadline:
    raise SystemExit("STOP: instance did not reach running state within five minutes.")
  time.sleep(5)
if refreshed.get("State") != "running" or refreshed.get("KeyName") != resources.get("key_name"):
    raise SystemExit("STOP: instance did not reach the expected running state.")
public_ip = refreshed.get("PublicIp")
if not isinstance(public_ip, str) or not re.fullmatch(r"[0-9.]+", public_ip):
    raise SystemExit("STOP: no current public IPv4 address is assigned.")
print(public_ip)
PY
)"
printf 'Refreshed SSH target: ec2-user@%s\n' "$PUBLIC_IP"

SSH_OPTIONS=(-i "$KEY_PATH" -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10)
ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" 'sudo bash -s' < "$USER_DATA_PATH"

REMOTE_INSTALL_COMMAND='sudo install -d -o root -g root -m 755 /etc/inspection && sudo tee /etc/inspection/app.env >/dev/null && sudo chown root:root /etc/inspection/app.env && sudo chmod 600 /etc/inspection/app.env && sudo install -d -o root -g root -m 755 /etc/systemd/system/inspection.service.d && printf "%s\n" "[Service]" "EnvironmentFile=/etc/inspection/app.env" | sudo tee /etc/systemd/system/inspection.service.d/app-env.conf >/dev/null && sudo chown root:root /etc/systemd/system/inspection.service.d/app-env.conf && sudo chmod 644 /etc/systemd/system/inspection.service.d/app-env.conf && test "$(sudo stat -c %U:%G:%a /etc/inspection/app.env)" = "root:root:600"'
ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" "$REMOTE_INSTALL_COMMAND" < "$APP_ENV_PATH"
printf 'Remote app.env owner and mode verified: root:root 600\n'
ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" 'sudo systemctl daemon-reload && sudo systemctl restart inspection && systemctl is-active --quiet inspection'

HEALTH_PATH="$(mktemp "${TMPDIR:-/tmp}/w04-health.XXXXXX")"
trap 'rm -f "$HEALTH_PATH"' EXIT
curl --fail --silent --show-error --max-time 8 "http://${PUBLIC_IP}/health" -o "$HEALTH_PATH"
python3 - "$COMMIT" "$HEALTH_PATH" <<'PY'
import json
import sys

expected_version, health_path = sys.argv[1:]
try:
    health = json.load(open(health_path, encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit("STOP: health endpoint returned invalid JSON.")
if health.get("version") != expected_version:
    raise SystemExit("STOP: health version does not match the deployed commit.")
if health.get("auth_configured") is not True:
    raise SystemExit("STOP: health endpoint reports auth_configured=false.")
print("Health verified: version matches commit and auth_configured=true.")
PY
printf 'W4 deployment complete for instance %s at %s.\n' "$INSTANCE_ID" "$PUBLIC_IP"
