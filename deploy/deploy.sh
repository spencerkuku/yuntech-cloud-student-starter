#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RESOURCES_FILE="$ROOT_DIR/.local/resources.json"
APP_ENV="$ROOT_DIR/.local/app.env"
INSTALLER="$ROOT_DIR/.local/w04-install.sh"
AWS_REGION="${AWS_REGION:-us-east-1}"
DEPLOY_COMMIT="${1:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"

aws_cmd() {
  AWS_PROFILE=learnerlab AWS_REGION="$AWS_REGION" AWS_DEFAULT_REGION="$AWS_REGION" \
    aws --no-cli-pager "$@"
}

[ -f "$RESOURCES_FILE" ] || { echo "找不到 .local/resources.json" >&2; exit 1; }
[ -f "$APP_ENV" ] || { echo "找不到 .local/app.env" >&2; exit 1; }

if [ "$(stat -c '%a' "$APP_ENV")" != "600" ]; then
  echo ".local/app.env 權限必須為 600" >&2
  exit 1
fi

DEPLOY_COMMIT="$(git -C "$ROOT_DIR" rev-parse --verify "${DEPLOY_COMMIT}^{commit}")"

INSTANCE_ID="$(python3 - "$RESOURCES_FILE" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text())
print(d.get("instance_id", ""))
PY
)"

KEY_NAME="$(python3 - "$RESOURCES_FILE" <<'PY'
import json, sys
from pathlib import Path
d = json.loads(Path(sys.argv[1]).read_text())
print(d.get("key_pair_name", ""))
PY
)"

[ -n "$INSTANCE_ID" ] || { echo "manifest 沒有 instance_id" >&2; exit 1; }
[ -n "$KEY_NAME" ] || { echo "manifest 沒有 key_pair_name" >&2; exit 1; }

KEY_PATH="$HOME/.ssh/$KEY_NAME"
[ -f "$KEY_PATH" ] || { echo "找不到 SSH private key" >&2; exit 1; }
[ "$(stat -c '%a' "$KEY_PATH")" = "600" ] || {
  echo "SSH private key 權限必須為 600" >&2
  exit 1
}

PUBLIC_IP="$(aws_cmd ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)"

[ -n "$PUBLIC_IP" ] && [ "$PUBLIC_IP" != "None" ] || {
  echo "EC2 沒有 public IP" >&2
  exit 1
}

# 與 up.sh 使用相同 packager，只打包 committed files。
bash "$ROOT_DIR/deploy/make-user-data.sh" "$DEPLOY_COMMIT" "$INSTALLER"

echo "=== W4 部署確認 ==="
echo "Instance: $INSTANCE_ID"
echo "Public IP: $PUBLIC_IP"
echo "Commit: $DEPLOY_COMMIT"
echo "將更新既有 inspection service，不建立新的 EC2 / SG / key pair。"
echo "確認後輸入: yes"
read -r response

if [ "$response" != "yes" ]; then
  echo "已取消，未部署。"
  exit 0
fi

SSH=(
  ssh
  -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes
  -i "$KEY_PATH"
  "ec2-user@$PUBLIC_IP"
)

# 安裝 committed W4 application。
"${SSH[@]}" 'sudo bash -s' < "$INSTALLER"

# Secret 只經 SSH stdin 傳送，不放入 CLI argument / user-data / Git。
cat "$APP_ENV" | "${SSH[@]}" \
  'sudo install -d -m 700 /etc/inspection &&
   sudo install -m 600 /dev/stdin /etc/inspection/app.env &&
   sudo chown root:root /etc/inspection/app.env'

# app.env 是在 installer 啟動 service 後才放入，因此再 restart 一次。
"${SSH[@]}" \
  'sudo systemctl daemon-reload &&
   sudo systemctl restart inspection &&
   sudo systemctl is-active --quiet inspection'

HEALTH_FILE="$ROOT_DIR/.local/w04-health.json"
HTTP_STATUS="$(curl --silent --show-error \
  --connect-timeout 8 --max-time 20 \
  -o "$HEALTH_FILE" -w '%{http_code}' \
  "http://$PUBLIC_IP/health")"

[ "$HTTP_STATUS" = "200" ] || {
  echo "/health HTTP=$HTTP_STATUS" >&2
  exit 1
}

python3 - "$HEALTH_FILE" "$DEPLOY_COMMIT" <<'PY'
import json, sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
expected = sys.argv[2]

if payload.get("status") != "ok":
    raise SystemExit("/health status != ok")
if payload.get("service") != "inspection":
    raise SystemExit("/health service != inspection")
if payload.get("version") != expected:
    raise SystemExit("/health version != deployment commit")
if payload.get("auth_configured") is not True:
    raise SystemExit("/health auth_configured != true")

print("W4 deployment verified")
print("version:", payload["version"])
print("auth_configured: true")
PY
