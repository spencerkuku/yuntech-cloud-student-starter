#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
LOCAL="$ROOT/practice/spencerku/w4/.local"
CONFIG="$LOCAL/w4.env"
APP_ENV="$LOCAL/app.env"
PACKAGER="$ROOT/practice/spencerku/w4/deploy/make_user_data.py"

fail() { printf 'STOP: %s\n' "$1" >&2; exit 1; }
[[ -f "$CONFIG" ]] || fail "missing $CONFIG"
[[ -f "$APP_ENV" ]] || fail "missing $APP_ENV"
mode="$(stat -c '%a' "$APP_ENV" 2>/dev/null || stat -f '%Lp' "$APP_ENV")"
[[ "$mode" == "600" ]] || fail "$APP_ENV must have mode 600"

# shellcheck disable=SC1090
source "$CONFIG"
: "${INSTANCE_ID:?w4.env must define INSTANCE_ID}"
: "${SSH_USER:?w4.env must define SSH_USER}"
: "${SSH_KEY_FILE:?w4.env must define SSH_KEY_FILE}"
DEPLOY_COMMIT="${DEPLOY_COMMIT:-HEAD}"
SSH_KEY_FILE="${SSH_KEY_FILE/#\~/$HOME}"
[[ -r "$SSH_KEY_FILE" ]] || fail "SSH key is not readable"

bash "$ROOT/scripts/verify-aws.sh"
HOST_IP="$(python3 - "$ROOT" "$INSTANCE_ID" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
instance_id = sys.argv[2]
resources = json.loads((root / "practice/spencerku/w3/.local/resources.json").read_text(encoding="utf-8"))
if resources.get("instance_id") != instance_id or resources.get("owner") != "spencerku":
  raise SystemExit("STOP: instance does not match the recorded W3 resource owner")
sys.path.insert(0, str(root / "scripts"))
import lab

context = lab.context()
result = lab.run_aws(["ec2", "describe-instances", "--instance-ids", instance_id], context["region"])
instances = result.get("Reservations", [{}])[0].get("Instances", [])
if len(instances) != 1 or instances[0].get("State", {}).get("Name") != "running":
  raise SystemExit("STOP: recorded W3 instance is not running")
public_ip = instances[0].get("PublicIpAddress")
if not public_ip:
  raise SystemExit("STOP: recorded W3 instance has no current public IP")
print(public_ip)
PY
)"

SHA="$(git -C "$ROOT" rev-parse --verify --end-of-options "$DEPLOY_COMMIT^{commit}")"
USER_DATA="$LOCAL/w04-user-data-$SHA.sh"
if [[ ! -e "$USER_DATA" ]]; then
  python3 "$PACKAGER" "$SHA" "$USER_DATA"
fi
printf 'Target host: %s\nCommit: %s\n' "$HOST_IP" "$SHA"
printf 'Type APPROVE to continue: '
read -r approval
[[ "$approval" == "APPROVE" ]] || fail "deployment cancelled"

SSH=(ssh -i "$SSH_KEY_FILE" -o StrictHostKeyChecking=accept-new -o BatchMode=yes "$SSH_USER@$HOST_IP")
"${SSH[@]}" 'sudo bash -s' < "$USER_DATA"
"${SSH[@]}" 'sudo install -d -m 755 /etc/inspection && sudo install -o root -g root -m 600 /dev/stdin /etc/inspection/app.env' < "$APP_ENV"
"${SSH[@]}" 'sudo systemctl restart inspection'

health="$(curl --fail --silent --show-error --max-time 10 "http://$HOST_IP/health")"
python3 - "$SHA" "$health" <<'PY'
import json
import sys
expected, raw = sys.argv[1:]
body = json.loads(raw)
if body.get("version") != expected or body.get("auth_configured") is not True:
    raise SystemExit("STOP: /health version or auth_configured mismatch")
print(json.dumps({"version": body["version"], "auth_configured": body["auth_configured"]}))
PY
