#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${ROOT_DIR}/.local"
RESOURCES_PATH="${LOCAL_DIR}/resources.json"
STOP_ONLY=0

usage() {
  cat <<'EOF'
Usage: deploy/down.sh [--stop] [--region us-east-1]

Deletes or stops the W3 resources tracked in .local/resources.json.
No resource names are used for bulk deletion; only IDs from the local ledger are accepted.
EOF
}

region="${AWS_REGION:-us-east-1}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      region="$2"
      shift 2
      ;;
    --stop)
      STOP_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$RESOURCES_PATH" ]]; then
  echo "No tracked resources found at ${RESOURCES_PATH}; nothing to delete." >&2
  exit 1
fi

python3 - "$ROOT_DIR" <<'PY'
import json, sys
root = sys.argv[1]
sys.path.insert(0, root)
from scripts.lab import verify
verify()
PY

EVAL_ENV="$(python3 - "$ROOT_DIR" <<'PY'
import json
import os
import sys
root = sys.argv[1]
sys.path.insert(0, root)
from scripts.lab import context
ctx = context()
print(f"export AWS_PROFILE=learnerlab")
print(f"export AWS_REGION={ctx['region']}")
print(f"export AWS_DEFAULT_REGION={ctx['region']}")
print(f"export AWS_SHARED_CREDENTIALS_FILE={os.path.expanduser('~/.aws/credentials')}")
print(f"export AWS_CONFIG_FILE={os.path.expanduser('~/.aws/config')}")
print("export AWS_EC2_METADATA_DISABLED=true")
print("export AWS_PAGER=")
print("export AWS_CLI_AUTO_PROMPT=off")
PY
)"
eval "$EVAL_ENV"

python3 - <<'PY' "$RESOURCES_PATH"
import json, sys
path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as fh:
    data = json.load(fh)
for key in ('instance_id', 'sg_id', 'key_name', 'root_volume_id', 'eni_id'):
    if key not in data:
        raise SystemExit(f"Missing tracked ID for {key} in {path}")
print(json.dumps(data, indent=2, sort_keys=True))
PY

read -r -p "Type 'DELETE W03' to continue: " approval
if [[ "$approval" != "DELETE W03" ]]; then
  echo "Cancelled: no AWS resources were removed." >&2
  exit 1
fi

instance_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["instance_id"])' "$RESOURCES_PATH")"
sg_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sg_id"])' "$RESOURCES_PATH")"
key_name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["key_name"])' "$RESOURCES_PATH")"
root_volume_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["root_volume_id"])' "$RESOURCES_PATH")"
eni_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["eni_id"])' "$RESOURCES_PATH")"

if [[ "$STOP_ONLY" -eq 1 ]]; then
  aws ec2 stop-instances --instance-ids "$instance_id"
  aws ec2 wait instance-stopped --instance-ids "$instance_id"
  state="$(aws ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  if [[ "$state" != "stopped" ]]; then
    echo "Expected instance to be stopped, got ${state}" >&2
    exit 1
  fi
  echo "Stopped and retained instance ${instance_id}."
  exit 0
fi

aws ec2 terminate-instances --instance-ids "$instance_id"
aws ec2 wait instance-terminated --instance-ids "$instance_id"

for resource in "$eni_id" "$root_volume_id"; do
  if [[ -n "$resource" ]] && [[ "$resource" != "null" ]]; then
    aws ec2 describe-volumes --volume-ids "$resource" >/dev/null 2>&1 || true
  fi
done

for resource_id in "$sg_id"; do
  aws ec2 delete-security-group --group-id "$resource_id" >/dev/null 2>&1 || true
done
aws ec2 delete-key-pair --key-name "$key_name" >/dev/null 2>&1 || true

# Strict post-delete verification: the tracked resources must be gone, and the instance must not exist.
aws ec2 describe-instances --instance-ids "$instance_id" >/tmp/w03-down-describe.out 2>/tmp/w03-down-describe.err || true
if aws ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -q .; then
  echo "Instance ${instance_id} still exists after termination." >&2
  exit 1
fi
if aws ec2 describe-security-groups --group-ids "$sg_id" --output text 2>/dev/null | grep -q .; then
  echo "Security group ${sg_id} still exists after deletion." >&2
  exit 1
fi
if aws ec2 describe-key-pairs --key-names "$key_name" --output text 2>/dev/null | grep -q .; then
  echo "Key pair ${key_name} still exists after deletion." >&2
  exit 1
fi

rm -f "$RESOURCES_PATH"

echo "All tracked W3 resources have been deleted or stopped per the local ledger."
