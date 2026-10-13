#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .local/app.env ]]; then
  echo "Missing .local/app.env; generate reporter/operator tokens first." >&2
  exit 1
fi
if [[ "$(stat -c '%a' .local/app.env 2>/dev/null || echo 000)" != "600" ]]; then
  echo "Refusing to deploy: .local/app.env must be mode 600" >&2
  exit 1
fi
if [[ ! -f .local/db.env || "$(stat -c '%a' .local/db.env 2>/dev/null || echo 000)" != "600" ]]; then
  echo "Refusing to deploy: restore .local/db.env from your secure backup with mode 600" >&2
  exit 1
fi

commit="$(git rev-parse HEAD)"
host="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('.local/resources.json')
if not path.exists():
    raise SystemExit('Missing .local/resources.json')
with path.open() as fh:
    data = json.load(fh)
print(data.get('host_public_ip') or data.get('public_ip') or '')
PY
)"
if [[ -z "$host" ]]; then
  echo "No EC2 public IP found in .local/resources.json" >&2
  exit 1
fi

python3 deploy/make_user_data.py "$commit" .local/user-data.sh >/dev/null

user="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('.local/resources.json')
if path.exists():
    try:
        data = json.loads(path.read_text())
        print(data.get('ssh_user') or data.get('user') or 'ec2-user')
        raise SystemExit
    except Exception:
        pass
print('ec2-user')
PY
)"

cleanup_remote() {
  ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" \
    'sudo rm -f /tmp/inspection-user-data.sh /tmp/inspection-app.env /tmp/inspection-db.env /tmp/AmazonRootCA1.pem /tmp/consumer.cert.pem /tmp/consumer.private.key' \
    >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

scp -p -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 .local/user-data.sh "$user@$host:/tmp/inspection-user-data.sh"
scp -p -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 .local/app.env "$user@$host:/tmp/inspection-app.env"
scp -p -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 .local/db.env "$user@$host:/tmp/inspection-db.env"
if [[ -d .local/iot ]]; then
  scp -p -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 .local/iot/AmazonRootCA1.pem .local/iot/consumer.cert.pem .local/iot/consumer.private.key "$user@$host:/tmp/"
fi
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'sudo install -d -m 755 /etc/inspection && sudo install -d -o inspection -g inspection -m 700 /etc/inspection/iot && sudo install -o root -g root -m 600 /tmp/inspection-app.env /etc/inspection/app.env && sudo sh -c "cat /tmp/inspection-db.env >> /etc/inspection/app.env" && sudo chmod 600 /etc/inspection/app.env && sudo chown root:root /etc/inspection/app.env && sudo install -o inspection -g inspection -m 600 /tmp/AmazonRootCA1.pem /etc/inspection/iot/AmazonRootCA1.pem && sudo install -o inspection -g inspection -m 600 /tmp/consumer.cert.pem /etc/inspection/iot/consumer.cert.pem && sudo install -o inspection -g inspection -m 600 /tmp/consumer.private.key /etc/inspection/iot/consumer.private.key && sudo rm -f /tmp/inspection-app.env /tmp/inspection-db.env /tmp/AmazonRootCA1.pem /tmp/consumer.cert.pem /tmp/consumer.private.key'
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'sudo bash /tmp/inspection-user-data.sh; status=$?; sudo rm -f /tmp/inspection-user-data.sh; exit "$status"'
sleep 2
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'curl -fsS http://127.0.0.1:8080/health' >/dev/null

printf 'Deploying commit %s to %s\n' "$commit" "$host"
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'sudo systemctl restart inspection && sudo systemctl restart inspection-consumer' >/dev/null 2>&1 || true
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'systemctl is-active inspection inspection-consumer' || true
