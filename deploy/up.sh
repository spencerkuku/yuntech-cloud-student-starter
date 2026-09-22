#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DIR="${ROOT_DIR}/.local"
RESOURCES_PATH="${LOCAL_DIR}/resources.json"
USER_DATA_PATH="${LOCAL_DIR}/w03-user-data.sh"

usage() {
  cat <<'EOF'
Usage: deploy/up.sh --commit <sha> --group <group> --owner <owner> --source-ip <ipv4> [--region us-east-1]

Creates the W3 EC2 deployment for a single learnerlab account.
All AWS calls use the learnerlab profile and the course-safe environment stored by scripts/lab.py.
EOF
}

region="${AWS_REGION:-us-east-1}"
commit=""
group=""
owner=""
source_ip="${SOURCE_IP:-}"
key_name=""
sg_name=""
instance_type="t3.micro"
approve_token=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      region="$2"
      shift 2
      ;;
    --commit)
      commit="$2"
      shift 2
      ;;
    --group)
      group="$2"
      shift 2
      ;;
    --owner)
      owner="$2"
      shift 2
      ;;
    --source-ip)
      source_ip="$2"
      shift 2
      ;;
    --key-name)
      key_name="$2"
      shift 2
      ;;
    --sg-name)
      sg_name="$2"
      shift 2
      ;;
    --instance-type)
      instance_type="$2"
      shift 2
      ;;
    --approve)
      approve_token="$2"
      shift 2
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

if [[ -z "$commit" ]]; then
  commit="$(git -C "$ROOT_DIR" rev-parse HEAD)"
fi
if [[ -z "$group" ]]; then
  echo "Missing required --group value." >&2
  exit 1
fi
if [[ -z "$owner" ]]; then
  echo "Missing required --owner value." >&2
  exit 1
fi
if [[ -z "$source_ip" ]]; then
  echo "Missing required --source-ip value. Example: 203.0.113.10" >&2
  exit 1
fi
if [[ ! "$source_ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "--source-ip must be a valid IPv4 address; got ${source_ip}" >&2
  exit 1
fi
if [[ -z "$key_name" ]]; then
  key_name="w03-${group}-${owner}-key"
fi
if [[ -z "$sg_name" ]]; then
  sg_name="w03-${group}-${owner}-sg"
fi
if [[ -z "$approve_token" ]]; then
  approve_token="I-APPROVE-W03"
fi

mkdir -p "$LOCAL_DIR"
chmod 700 "$LOCAL_DIR"

# Validate repo state and generate the exact user-data payload for the selected commit.
if ! git -C "$ROOT_DIR" rev-parse --verify "${commit}^{commit}" >/dev/null 2>&1; then
  echo "Commit not found: ${commit}" >&2
  exit 1
fi
bash "$ROOT_DIR/deploy/make-user-data.sh" "$commit" "$USER_DATA_PATH"

# Import the course-safe AWS environment, created outside the repository.
EVAL_ENV="$(python3 - "$ROOT_DIR" <<'PY'
import json
import os
import sys
root = sys.argv[1]
sys.path.insert(0, root)
from scripts.lab import context, verify
ctx = verify()
print(f"export AWS_PROFILE={ctx['profile'] if 'profile' in ctx else 'learnerlab'}")
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

# Print the exact plan for human review; no automatic approval.
cat <<EOF
W3 deployment plan
-------------------
Region: ${region}
Commit: ${commit}
Group: ${group}
Owner: ${owner}
Source CIDR: ${source_ip}/32
Security group: ${sg_name}
EC2 key pair: ${key_name}
Instance type: ${instance_type}
User data payload: ${USER_DATA_PATH}
Resources file: ${RESOURCES_PATH}
EOF

if [[ -n "${approve_token}" ]]; then
  read -r -p "Type '${approve_token}' to create the resources: " approval
  if [[ "$approval" != "$approve_token" ]]; then
    echo "Cancelled: no AWS resources created." >&2
    exit 1
  fi
fi

# Ensure the default VPC/subnet checks are done before launch.
vpc_id="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
if [[ -z "$vpc_id" || "$vpc_id" == "None" ]]; then
  echo "No default VPC was found in region ${region}; stop and verify the Learner Lab environment." >&2
  exit 1
fi
subnet_id="$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$vpc_id" --query 'Subnets[0].SubnetId' --output text)"
if [[ -z "$subnet_id" || "$subnet_id" == "None" ]]; then
  echo "No default public subnet was found in VPC ${vpc_id}; stop and verify the network layout." >&2
  exit 1
fi
route_table_id="$(aws ec2 describe-route-tables --filters Name=vpc-id,Values="$vpc_id" Name=association.main,Values=true --query 'RouteTables[0].RouteTableId' --output text)"
if [[ -z "$route_table_id" || "$route_table_id" == "None" ]]; then
  route_table_id="$(aws ec2 describe-subnets --subnet-ids "$subnet_id" --query 'Subnets[0].RouteTableId' --output text)"
fi
if [[ -n "$route_table_id" && "$route_table_id" != "None" ]]; then
  aws ec2 describe-route-tables --route-table-ids "$route_table_id" --query 'RouteTables[0].Routes[?DestinationCidrBlock==`0.0.0.0/0`].GatewayId' --output text | grep -q "igw-" || true
fi

# Create the security group with only the learner source CIDR allowed for TCP 22 and 80.
sg_id="$(aws ec2 create-security-group --group-name "$sg_name" --description "W3 inspection service" --vpc-id "$vpc_id" --query 'GroupId' --output text)"
aws ec2 authorize-security-group-ingress --group-id "$sg_id" --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${source_ip}/32}]" "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=${source_ip}/32}]"

# Create a dedicated key pair for SSH access, leaving the private key only in the Codespace.
mkdir -p "${HOME}/.ssh"
chmod 700 "${HOME}/.ssh"
ssh-keygen -t ed25519 -N "" -f "${HOME}/.ssh/${key_name}" -q || true
aws ec2 import-key-pair --key-name "$key_name" --public-key-material "$(cat "${HOME}/.ssh/${key_name}.pub")"

# Select the AL2023 x86_64 AMI, at least one public IPv4 address and user data packaging.
ami_id="$(aws ec2 describe-images \
  --owners amazon \
  --filters Name=name,Values='al2023-ami-*' Name architecture,Values='x86_64' Name=state,Values='available' \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)"
if [[ -z "$ami_id" || "$ami_id" == "None" ]]; then
  echo "Could not find an AL2023 x86_64 AMI in region ${region}; stop and verify the image selection." >&2
  exit 1
fi

instance_id="$(aws ec2 run-instances \
  --image-id "$ami_id" \
  --count 1 \
  --instance-type "$instance_type" \
  --key-name "$key_name" \
  --subnet-id "$subnet_id" \
  --security-group-ids "$sg_id" \
  --associate-public-ip-address \
  --tag-specifications "ResourceType=instance,Tags=[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=${group}},{Key=owner,Value=${owner}}]" \
  --user-data "file://${USER_DATA_PATH}" \
  --query 'Instances[0].InstanceId' --output text)"

# Gather instance metadata immediately after launch for the resource ledger.
aws ec2 wait instance-running --instance-ids "$instance_id"
instance_data="$(aws ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0]' --output json)"
root_volume_id="$(printf '%s' "$instance_data" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["BlockDeviceMappings"][0]["Ebs"]["VolumeId"])')"
eni_id="$(printf '%s' "$instance_data" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["NetworkInterfaces"][0]["NetworkInterfaceId"])')"
public_ip="$(printf '%s' "$instance_data" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("PublicIpAddress") or "")')"

# Record resource IDs so next steps and down.sh only operate on the owned IDs.
python3 - "$RESOURCES_PATH" "$sg_id" "$key_name" "$instance_id" "$root_volume_id" "$eni_id" <<'PY'
import json, sys
path = sys.argv[1]
sg_id, key_name, instance_id, root_volume_id, eni_id = sys.argv[2:7]
obj = {
    "sg_id": sg_id,
    "key_name": key_name,
    "instance_id": instance_id,
    "root_volume_id": root_volume_id,
    "eni_id": eni_id,
    "created_at_utc": __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    "group": None,
    "owner": None,
}
if path.exists():
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            prev = json.load(fh)
    except json.JSONDecodeError:
        prev = {}
    obj.update(prev)
with open(path, 'w', encoding='utf-8') as fh:
    json.dump(obj, fh, indent=2, sort_keys=True)
    fh.write('\n')
PY

# Post-launch HTTP check: first failure is expected if cloud-init is still completing.
if [[ -z "$public_ip" ]]; then
  echo "Instance is running but no public IP is currently assigned; do not treat the service as healthy yet." >&2
fi
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 8 "http://${public_ip}/health" >/tmp/w03-health.json 2>/tmp/w03-health.err; then
    break
  fi
  echo "Health probe failed before service readiness (attempt ${attempt}/30)."
  sleep 5
  if [[ $attempt -eq 30 ]]; then
    echo "The service did not become ready in the expected window; see /tmp/w03-health.err" >&2
  fi
done

curl -fsS --max-time 8 "http://${public_ip}/health" > /tmp/w03-health.json
python3 - <<'PY'
import json, sys
with open('/tmp/w03-health.json', 'r', encoding='utf-8') as fh:
    payload = json.load(fh)
assert payload.get('status') == 'ok', payload
assert payload.get('service') == 'inspection', payload
assert len(payload.get('version', '')) == 40, payload
print(f"Health OK: {payload}")
PY

# Final script success gate.
current_version="$(curl -fsS --max-time 8 "http://${public_ip}/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
if [[ "$current_version" != "$commit" ]]; then
  echo "Version mismatch: expected ${commit}, got ${current_version}" >&2
  exit 1
fi

echo "Deployment complete: instance ${instance_id} on ${public_ip} is healthy and reporting commit ${commit}."
