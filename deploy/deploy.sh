#!/usr/bin/env bash
# deploy/deploy.sh — W4: update the ALREADY RUNNING host in place.
#   1. packages a committed commit with deploy/make-user-data.sh (the same packer up.sh uses)
#   2. runs that install script over SSH, then installs .local/app.env through SSH stdin as
#      root/600, then restarts inspection so the service really reads the new secrets
#   3. finishes by checking /health version == this commit and auth_configured == true
#   4. prints the target host and the commit, then waits for your confirmation
# Tokens are never printed, never passed as command arguments, and never enter user data or Git.
# This script only ever touches the one instance recorded in .local/resources.json.
set -euo pipefail
set +x   # never trace: tracing would echo the secret file into the terminal

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CONFIG="$ROOT/.local/config"
RESOURCES="$ROOT/.local/resources.json"
SECRET="$ROOT/.local/app.env"
USER_DATA="$ROOT/.local/w04-user-data.sh"
SSH_USER="ec2-user"
PROBE_ATTEMPTS=20
PROBE_WAIT=3

die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- local preconditions
[ -f "$CONFIG" ]    || die "$CONFIG missing (SOURCE_IP/GROUP/OWNER)."
[ -f "$RESOURCES" ] || die "$RESOURCES missing; the W3 host to update is unknown."

cfg() { sed -n "s/^$1=//p" "$CONFIG" | head -1; }
OWNER="$(cfg OWNER)"
[ -n "$OWNER" ] || die "OWNER missing in $CONFIG."
KEY_NAME="w03-${OWNER}-key"
KEY_PATH="$HOME/.ssh/$KEY_NAME"
[ -f "$KEY_PATH" ] || die "private key $KEY_PATH not found; rebuild the host with deploy/up.sh."

# Spec: stop unless the local secret file is 600 before anything is uploaded.
[ -f "$SECRET" ] || die "$SECRET missing. Create it yourself with umask 077 (do not paste tokens here)."
SECRET_MODE="$(stat -c '%a' "$SECRET")"
[ "$SECRET_MODE" = "600" ] || die "$SECRET mode is $SECRET_MODE, not 600. Refusing to deploy."
for key in REPORTER_TOKEN OPERATOR_TOKEN; do
  grep -qE "^${key}=.+" "$SECRET" || die "$SECRET has no non-empty $key line."
done
REPORTER_LEN="$(sed -n 's/^REPORTER_TOKEN=//p' "$SECRET" | head -1 | tr -d '\n' | wc -c)"
OPERATOR_LEN="$(sed -n 's/^OPERATOR_TOKEN=//p' "$SECRET" | head -1 | tr -d '\n' | wc -c)"
[ "$REPORTER_LEN" -gt 16 ] || die "REPORTER_TOKEN looks empty."
[ "$OPERATOR_LEN" -gt 16 ] || die "OPERATOR_TOKEN looks empty."

COMMIT="$(cfg DEPLOY_COMMIT)"
[ -n "$COMMIT" ] || COMMIT="$(git rev-parse --verify "HEAD^{commit}")"
printf '%s' "$COMMIT" | grep -qE '^[0-9a-f]{40}$' || die "commit must be a 40-character SHA, got '$COMMIT'."
git cat-file -e "$COMMIT^{commit}" 2>/dev/null || die "commit $COMMIT is not in this repository."

DIRTY="$(git status --porcelain -- app deploy tests)"
[ -z "$DIRTY" ] || printf 'NOTE: uncommitted changes under app/ deploy/ tests/ are NOT deployed:\n%s\n' "$DIRTY"

# ---------------------------------------------------------------- control plane
bash scripts/verify-aws.sh || die "identity gate failed."

ROUND_ROW="$(python3 - <<'PY'
import json, sys
rounds = json.load(open(".local/resources.json"))["rounds"]
live = [r for r in rounds if r.get("instance", {}).get("state") != "terminated"]
if not live:
    sys.exit("no live round in .local/resources.json")
row = live[-1]
print(row["instance"]["id"], row["instance"]["state"], row.get("security_group", {}).get("id", "-"))
PY
)" || die "no live round in .local/resources.json."
read -r INSTANCE_ID STATE SG_ID <<< "$ROUND_ROW"

PUBLIC_IP="$(python3 - "$INSTANCE_ID" <<'PY'
import sys
sys.path.insert(0, "scripts")
# The identity gate already ran above via scripts/verify-aws.sh. Use context() here,
# not verify(), because verify() prints a line that would pollute this captured value.
from lab import run_aws, context, LabError
try:
    ctx = context()
    # --query already projects the response, so the result IS the state/IP pair.
    row = run_aws(["ec2", "describe-instances", "--instance-ids", sys.argv[1],
                   "--query", "Reservations[0].Instances[0].{S:State.Name,P:PublicIpAddress}"],
                  ctx["region"])
except (LabError, TypeError) as exc:
    sys.exit(f"AWS lookup failed: {exc}")
if not isinstance(row, dict) or "P" not in row:
    sys.exit("AWS returned no address for this instance")
print(row["P"])
sys.exit(0 if row.get("S") == "running" and row["P"] else 3)
PY
)" || die "instance $INSTANCE_ID is not running with a public IPv4; use deploy/up.sh instead."

printf '\n===== Deploy plan (nothing has changed yet) =====\n'
printf ' target instance : %s (state=%s, sg=%s)\n' "$INSTANCE_ID" "$STATE" "$SG_ID"
printf ' current public  : http://%s   (re-queried just now, not the recorded one)\n' "$PUBLIC_IP"
printf ' deploy commit   : %s\n' "$COMMIT"
printf ' secret file     : %s (mode 600, %s token(s) present, values never printed)\n' "$SECRET" 2
printf ' restart         : inspection service is restarted again after the secret is installed\n'
printf ' cost            : no new resource; only a running host that already exists\n'
printf ' rollback        : deploy the previous commit the same way, or stop the host with deploy/down.sh --stop\n'

[ -t 0 ] || die "no interactive terminal. Run this yourself in your Codespace terminal."
printf '\n'
read -r -p "Type exactly DEPLOY to update $INSTANCE_ID with $COMMIT: " ANSWER
[ "$ANSWER" = "DEPLOY" ] || die "cancelled; nothing was changed."

# ---------------------------------------------------------------- 1. package the commit
rm -f "$USER_DATA"
bash deploy/make-user-data.sh "$COMMIT" "$USER_DATA" || die "packaging failed."

SSH=(ssh -i "$KEY_PATH" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new
     -o ConnectTimeout=10 -o ServerAliveInterval=15 "$SSH_USER@$PUBLIC_IP")

# ---------------------------------------------------------------- 2a. install the code
printf '\n[1/4] running the install script over SSH ...\n'
"${SSH[@]}" 'sudo bash -s' < "$USER_DATA" || die "install script failed on $PUBLIC_IP."

# ---------------------------------------------------------------- 2b. install the secret
# Through stdin, so the token never appears in a command line, a process list or this log.
printf '[2/4] installing %s as root/600 through SSH stdin ...\n' "$SECRET"
REMOTE_SECRET_CMD="sudo sh -c 'umask 077; install -d -m 700 /etc/inspection; cat > /etc/inspection/app.env; chmod 600 /etc/inspection/app.env; chown root:root /etc/inspection/app.env'"
"${SSH[@]}" "$REMOTE_SECRET_CMD" < "$SECRET" \
  || die "secret file could not be installed."

# ---------------------------------------------------------------- 2c. restart so it is read
printf '[3/4] restarting inspection so the service reads the new secrets ...\n'
"${SSH[@]}" 'sudo systemctl restart inspection && sudo systemctl is-active inspection' \
  || die "inspection did not come back up."

# ---------------------------------------------------------------- 3. verify
printf '[4/4] checking /health ...\n'
for attempt in $(seq 1 "$PROBE_ATTEMPTS"); do
  HEALTH="$(curl -sS --max-time 8 "http://$PUBLIC_IP/health" 2>/dev/null || true)"
  if printf '%s' "$HEALTH" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
sys.exit(0 if body.get("version") == sys.argv[1] and body.get("auth_configured") is True else 1)
' "$COMMIT"; then
    printf '\nOK  version == %s and auth_configured == true\n' "$COMMIT"
    printf '    %s\n' "$HEALTH"
    printf '\nNext: run the rejection matrix (tests/reject_matrix.py), then open http://%s/ and\n' "$PUBLIC_IP"
    printf 'paste the OPERATOR token yourself. The token stays out of the URL and out of this log.\n'
    exit 0
  fi
  printf '  attempt %s/%s: not ready yet\n' "$attempt" "$PROBE_ATTEMPTS"
  sleep "$PROBE_WAIT"
done

printf '\nSTOP: /health never reported version %s with auth_configured true.\n' "$COMMIT"
printf 'The code is installed; check the host before retrying:\n'
printf '  ssh -i %s %s@%s "sudo systemctl status inspection --no-pager"\n' "$KEY_PATH" "$SSH_USER" "$PUBLIC_IP"
printf '  ssh -i %s %s@%s "sudo ls -l /etc/inspection/app.env"   # prints metadata, never contents\n' "$KEY_PATH" "$SSH_USER" "$PUBLIC_IP"
exit 1
