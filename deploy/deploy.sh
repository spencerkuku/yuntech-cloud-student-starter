#!/usr/bin/env bash
set -euo pipefail
set +x

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${ROOT_DIR}/.local"
RESOURCES_PATH="${LOCAL_DIR}/resources.json"
APP_ENV_PATH="${LOCAL_DIR}/app.env"
DB_ENV_PATH="${LOCAL_DIR}/db.env"
IOT_DIR="${LOCAL_DIR}/iot"
DEPLOY_APP_ENV_PATH=""
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
allowed = {
  "REPORTER_TOKEN", "OPERATOR_TOKEN", "IOT_ENDPOINT", "IOT_TOPIC",
  "IOT_CLIENT_ID", "IOT_DEVICE_IDS", "IOT_CA", "IOT_CERT", "IOT_KEY",
}
values = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
  line = line.strip()
  if not line or line.startswith("#"):
    continue
  key, separator, value = line.partition("=")
  if not separator or key not in allowed or key in values:
    raise SystemExit("STOP: .local/app.env contains an unknown or duplicate setting.")
  value = value.strip()
  if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
    value = value[1:-1]
  if not value or (key.endswith("_TOKEN") and re.search(r"\s", value)):
    raise SystemExit("STOP: .local/app.env contains an empty or invalid setting.")
  values[key] = value
if not {"REPORTER_TOKEN", "OPERATOR_TOKEN"} <= values.keys():
  raise SystemExit("STOP: .local/app.env must contain the W5 reporter and operator token settings.")
PY

python3 - "$IOT_DIR" <<'PY'
import stat
import sys
from pathlib import Path

iot_dir = Path(sys.argv[1])
if iot_dir.is_symlink() or not iot_dir.is_dir() or stat.S_IMODE(iot_dir.stat().st_mode) != 0o700:
    raise SystemExit("STOP: .local/iot must be a non-symlink directory with mode 700.")
for name in ("endpoint", "consumer.cert.pem", "consumer.private.key", "AmazonRootCA1.pem"):
    path = iot_dir / name
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("STOP: required .local/iot files must be regular non-symlink files with mode 600.")
PY

if [[ ! -f "$DB_ENV_PATH" || -L "$DB_ENV_PATH" ]]; then
  echo 'STOP: .local/db.env is missing or not a regular file.' >&2
  exit 1
fi
python3 - "$DB_ENV_PATH" <<'PY'
import stat
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
if stat.S_IMODE(env_path.stat().st_mode) != 0o600:
    raise SystemExit("STOP: .local/db.env permissions must be exactly 600.")
required = {"DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"}
values = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    key, separator, value = line.partition("=")
    if not separator or key not in required or key in values or not value.strip():
        raise SystemExit("STOP: .local/db.env has an invalid or duplicate setting.")
    values[key] = value.strip()
if values.keys() != required:
    raise SystemExit("STOP: .local/db.env must contain all four DB settings.")
PY

DEPLOY_ENV_DIR="$(mktemp -d "${TMPDIR:-/tmp}/w06-app-env.XXXXXX")"
chmod 700 "$DEPLOY_ENV_DIR"
DEPLOY_APP_ENV_PATH="${DEPLOY_ENV_DIR}/app.env"
trap 'rm -f "$DEPLOY_APP_ENV_PATH"; rmdir "$DEPLOY_ENV_DIR"' EXIT
python3 - "$APP_ENV_PATH" "$DB_ENV_PATH" "$IOT_DIR/endpoint" "$DEPLOY_APP_ENV_PATH" <<'PY'
import re
import sys
from pathlib import Path

app_env_path, db_env_path, endpoint_path, destination = map(Path, sys.argv[1:])
app_lines = app_env_path.read_text(encoding="utf-8").splitlines()
db_lines = db_env_path.read_text(encoding="utf-8").splitlines()
base_lines = [
    line for line in app_lines
    if not line.partition("=")[0].strip().startswith("IOT_")
]
endpoint = endpoint_path.read_text(encoding="ascii").strip()
if not re.fullmatch(r"[A-Za-z0-9.-]+\.iot\.us-east-1\.amazonaws\.com", endpoint):
    raise SystemExit("STOP: .local/iot/endpoint is not a valid us-east-1 IoT endpoint.")
devices = ",".join(f"g02-lin-d{i:02d}" for i in range(1, 11))
values = {
    "IOT_ENDPOINT": endpoint,
    "IOT_TOPIC": "cxe/g02-lin/gw/events",
    "IOT_CLIENT_ID": "consumer-g02-lin",
    "IOT_DEVICE_IDS": devices,
    "IOT_CA": "/etc/inspection/iot/AmazonRootCA1.pem",
    "IOT_CERT": "/etc/inspection/iot/consumer.cert.pem",
    "IOT_KEY": "/etc/inspection/iot/consumer.private.key",
}
with destination.open("w", encoding="utf-8") as stream:
    stream.write("\n".join(base_lines).rstrip() + "\n")
    stream.write("\n".join(db_lines).rstrip() + "\n")
    for key, value in values.items():
        stream.write(f"{key}={value}\n")
PY

if [[ ! -f "$RESOURCES_PATH" || -L "$RESOURCES_PATH" ]]; then
  echo 'STOP: .local/resources.json is missing or not a regular file.' >&2
  exit 1
fi

# The package builder reads committed app/service.py and deploy/nginx.conf only.
for source in app/service.py app/consumer.py deploy/nginx.conf deploy/make_user_data.py deploy/make-user-data.sh deploy/deploy.sh; do
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
python3 - "$APP_ENV_PATH" "$DB_ENV_PATH" "$USER_DATA_PATH" <<'PY'
import sys
from pathlib import Path

app_env_path, db_env_path, user_data_path = map(Path, sys.argv[1:])
secrets = []
for env_path in (app_env_path, db_env_path):
  for line in env_path.read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator and value and key in {
        "REPORTER_TOKEN", "OPERATOR_TOKEN", "DB_PASSWORD", "DB_HOST"
    }:
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
from scripts.resources import load_deployment_target, ResourceLedgerError

ctx = context()
identity = run_aws(["sts", "get-caller-identity"], ctx["region"])
if identity.get("Account") != ctx["account"] or ":assumed-role/" not in identity.get("Arn", ""):
    raise SystemExit("STOP: learnerlab identity does not match its saved context.")
try:
  target = load_deployment_target(resources_path)
except ResourceLedgerError as exc:
  raise SystemExit(f"STOP: {exc}.") from None
instance_id = target["instance_id"]
key_name = target["key_name"]
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

printf 'W5 deployment target\n'
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
read -r -p "Type 'DEPLOY W5' to continue: " approval
if [[ "$approval" != 'DEPLOY W5' ]]; then
  echo 'Cancelled; no deployment started.' >&2
  exit 1
fi

PUBLIC_IP="$(python3 - "$ROOT_DIR" "$INSTANCE_ID" "$KEY_NAME" <<'PY'
import json
import re
import sys
import time

root, instance_id, key_name = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lab import context, run_aws

ctx = context()
response = run_aws([
    "ec2", "describe-instances", "--instance-ids", instance_id,
    "--query", "Reservations[0].Instances[0].{State:State.Name,KeyName:KeyName}"
], ctx["region"])
if response.get("KeyName") != key_name:
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
if refreshed.get("State") != "running" or refreshed.get("KeyName") != key_name:
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

REMOTE_INSTALL_COMMAND='sudo install -d -o root -g root -m 755 /etc/inspection && sudo chmod 755 /etc/inspection && sudo install -d -o inspection -g inspection -m 700 /etc/inspection/iot && sudo install -d -o root -g root -m 755 /etc/systemd/system/inspection.service.d && printf "%s\n" "[Service]" "EnvironmentFile=/etc/inspection/app.env" | sudo tee /etc/systemd/system/inspection.service.d/app-env.conf >/dev/null && sudo chown root:root /etc/systemd/system/inspection.service.d/app-env.conf && sudo chmod 644 /etc/systemd/system/inspection.service.d/app-env.conf && test "$(sudo stat -c %U:%G:%a /etc/inspection)" = "root:root:755" && test "$(sudo stat -c %U:%G:%a /etc/inspection/iot)" = "inspection:inspection:700"'
ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" "$REMOTE_INSTALL_COMMAND"
printf 'Remote IoT directory owner and mode verified.\n'

REMOTE_IOT_INSTALL_SCRIPT="$(cat <<'REMOTE_IOT_INSTALL'
set -eu
umask 077
tar -xf - -C /etc/inspection/iot
chown inspection:inspection \
  /etc/inspection/iot/consumer.cert.pem \
  /etc/inspection/iot/consumer.private.key \
  /etc/inspection/iot/AmazonRootCA1.pem
chmod 600 \
  /etc/inspection/iot/consumer.cert.pem \
  /etc/inspection/iot/consumer.private.key \
  /etc/inspection/iot/AmazonRootCA1.pem
test "$(stat -c %U:%G:%a /etc/inspection)" = root:root:755
test "$(stat -c %U:%G:%a /etc/inspection/iot)" = inspection:inspection:700
test "$(stat -c %U:%G:%a /etc/inspection/iot/consumer.cert.pem)" = inspection:inspection:600
test "$(stat -c %U:%G:%a /etc/inspection/iot/consumer.private.key)" = inspection:inspection:600
test "$(stat -c %U:%G:%a /etc/inspection/iot/AmazonRootCA1.pem)" = inspection:inspection:600
REMOTE_IOT_INSTALL
)"
printf -v REMOTE_IOT_INSTALL_COMMAND 'sudo sh -c %q' "$REMOTE_IOT_INSTALL_SCRIPT"
tar -C "$IOT_DIR" -cf - consumer.cert.pem consumer.private.key AmazonRootCA1.pem |
  ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" "$REMOTE_IOT_INSTALL_COMMAND"
printf 'Consumer certificate, key and CA installed with verified owner and mode.\n'

REMOTE_ENV_SCRIPT="$(cat <<'REMOTE_ENV'
set -eu
umask 077
tmp="$(mktemp /etc/inspection/.app.env.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
tar -xOf - app.env > "$tmp"
chown root:root "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" /etc/inspection/app.env
trap - EXIT
test "$(stat -c %U:%G:%a /etc/inspection/app.env)" = root:root:600
REMOTE_ENV
)"
printf -v REMOTE_ENV_COMMAND 'sudo sh -c %q' "$REMOTE_ENV_SCRIPT"
tar -C "$DEPLOY_ENV_DIR" -cf - app.env |
  ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" "$REMOTE_ENV_COMMAND"
printf 'Remote app.env replaced atomically and verified as root:root 600.\n'

ssh "${SSH_OPTIONS[@]}" "ec2-user@${PUBLIC_IP}" 'sudo systemctl daemon-reload && sudo systemctl restart inspection && sudo systemctl enable inspection-consumer && sudo systemctl restart inspection-consumer && systemctl is-active inspection inspection-consumer'

HEALTH_PATH="$(mktemp "${TMPDIR:-/tmp}/w04-health.XXXXXX")"
trap 'rm -f "$HEALTH_PATH" "$DEPLOY_APP_ENV_PATH"; rmdir "$DEPLOY_ENV_DIR"' EXIT
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
if health.get("db_configured") is not True:
  raise SystemExit("STOP: health endpoint reports db_configured=false.")
print("Health verified: version, auth and database configuration match.")
PY
printf 'W6 deployment complete for instance %s at %s.\n' "$INSTANCE_ID" "$PUBLIC_IP"
