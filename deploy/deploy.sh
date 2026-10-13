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

scp -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 .local/user-data.sh "$user@$host:/tmp/inspection-user-data.sh"
scp -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 .local/app.env "$user@$host:/tmp/inspection-app.env"
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'sudo install -d -m 755 /etc/inspection && sudo install -o root -g root -m 600 /tmp/inspection-app.env /etc/inspection/app.env'
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'sudo bash /tmp/inspection-user-data.sh'
sleep 2
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'curl -fsS http://127.0.0.1:8080/health' >/dev/null

printf 'Deploying commit %s to %s\n' "$commit" "$host"
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'sudo systemctl restart inspection && sudo systemctl restart inspection-consumer' >/dev/null 2>&1 || true
ssh -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_ed25519 "$user@$host" 'systemctl is-active inspection inspection-consumer' || true
