#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p .local/iot
chmod 700 .local
chmod 700 .local/iot

if [[ ! -f .local/resources.json ]]; then
  echo ".local/resources.json is required before running IoT setup" >&2
  exit 1
fi

code="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('.local/resources.json')
if not path.exists():
    raise SystemExit(1)
with path.open() as fh:
    data = json.load(fh)
print(data.get('iot_code') or data.get('code') or '')
PY
)"
if [[ -z "$code" ]]; then
  echo "Set the course code in .local/resources.json before running IoT setup" >&2
  exit 1
fi

aws iot describe-endpoint --endpoint-type iot:Data-ATS --query 'endpointAddress' --output text > .local/iot/endpoint
account_id="$(aws sts get-caller-identity --query Account --output text)"

if [[ ! -f .local/iot/AmazonRootCA1.pem ]]; then
  curl --fail --silent --show-error -L https://www.amazontrust.com/repository/AmazonRootCA1.pem -o .local/iot/AmazonRootCA1.pem
fi

if [[ ! -f .local/iot/gw.cert.pem || ! -f .local/iot/gw.private.key ]]; then
  aws iot create-keys-and-certificate --set-as-active --certificate-pem-outfile .local/iot/gw.cert.pem --private-key-outfile .local/iot/gw.private.key > /tmp/gw-cert.json
  gw_cert_id="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('/tmp/gw-cert.json')
if path.exists():
    data = json.loads(path.read_text())
    print(data.get('certificateId', ''))
PY
)"
  if [[ -n "$gw_cert_id" ]]; then
    python3 - <<'PY'
import json
from pathlib import Path
path = Path('.local/resources.json')
data = json.loads(path.read_text()) if path.exists() else {}
data['iot_gw_cert_id'] = data.get('iot_gw_cert_id', '')
path.write_text(json.dumps(data, indent=2) + '\n')
PY
  fi
fi

if [[ ! -f .local/iot/consumer.cert.pem || ! -f .local/iot/consumer.private.key ]]; then
  aws iot create-keys-and-certificate --set-as-active --certificate-pem-outfile .local/iot/consumer.cert.pem --private-key-outfile .local/iot/consumer.private.key > /tmp/consumer-cert.json
fi

for policy in cxe-${code}-gw cxe-${code}-consumer; do
  if ! aws iot get-policy --policy-name "$policy" >/dev/null 2>&1; then
    case "$policy" in
      cxe-${code}-gw)
        aws iot create-policy --policy-name "$policy" --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["iot:Connect","iot:Publish"],"Resource":["arn:aws:iot:us-east-1:'"$account_id"':client/gw-'"$code"'","arn:aws:iot:us-east-1:'"$account_id"':topic/cxe/'"$code"'/gw/events"]}]}' >/dev/null
        ;;
      cxe-${code}-consumer)
        aws iot create-policy --policy-name "$policy" --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["iot:Connect","iot:Subscribe","iot:Receive"],"Resource":["arn:aws:iot:us-east-1:'"$account_id"':client/consumer-'"$code"'","arn:aws:iot:us-east-1:'"$account_id"':topicfilter/cxe/'"$code"'/gw/events","arn:aws:iot:us-east-1:'"$account_id"':topic/cxe/'"$code"'/gw/events"]}]}' >/dev/null
        ;;
    esac
  fi
done

aws iot attach-policy --policy-name "cxe-${code}-gw" --target "$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('/tmp/gw-cert.json')
if path.exists():
    data = json.loads(path.read_text())
    print(data.get('certificateArn', ''))
PY
)" >/dev/null 2>&1 || true
aws iot attach-policy --policy-name "cxe-${code}-consumer" --target "$(python3 - <<'PY'
import json
from pathlib import Path
path = Path('/tmp/consumer-cert.json')
if path.exists():
    data = json.loads(path.read_text())
    print(data.get('certificateArn', ''))
PY
)" >/dev/null 2>&1 || true

chmod 600 .local/iot/*.pem .local/iot/*.key 2>/dev/null || true
printf 'IoT setup complete for %s\n' "$code"
