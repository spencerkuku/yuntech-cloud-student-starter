#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${ROOT_DIR}/.local/up.env}"
RESOURCES_FILE="${ROOT_DIR}/.local/resources.json"
OBSERVATIONS_FILE="${ROOT_DIR}/.local/t2-observations.json"

if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
SOURCE_CIDR="${SOURCE_CIDR:-}"
GROUP_NAME="${GROUP_NAME:-}"
OWNER="${OWNER:-}"
DEPLOY_COMMIT="${DEPLOY_COMMIT:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
VPC_ID="${VPC_ID:-}"
SUBNET_ID="${SUBNET_ID:-}"
AMI_ID="${AMI_ID:-}"
KEY_NAME="${KEY_NAME:-}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
CREATED_SECURITY_GROUP_ID=""
IMPORTED_KEY_PAIR_NAME=""
RESUME_EXISTING_RESOURCES=false
RESUMED_SECURITY_GROUP_ID=""
RESUMED_KEY_PAIR_NAME=""
RESUMED_KEY_PAIR_ID=""

aws_cmd() {
  AWS_PROFILE=learnerlab \
  AWS_REGION="$AWS_REGION" \
  AWS_DEFAULT_REGION="$AWS_REGION" \
  AWS_SHARED_CREDENTIALS_FILE="$HOME/.aws/credentials" \
  AWS_CONFIG_FILE="$HOME/.aws/config" \
  AWS_PAGER="" \
  AWS_EC2_METADATA_DISABLED=true \
  AWS_CLI_AUTO_PROMPT=off \
  aws --profile learnerlab --region "$AWS_REGION" --no-cli-pager --output json "$@"
}

write_resources_json() {
  python3 - "$RESOURCES_FILE" "$@" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
items = json.loads(path.read_text()) if path.exists() else {}
if not isinstance(items, dict):
  raise SystemExit("resources.json must contain a JSON object; refusing to overwrite it")
key = sys.argv[2]
value = sys.argv[3]
items[key] = value
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

write_observation() {
  python3 - "$OBSERVATIONS_FILE" "$@" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
items = json.loads(path.read_text()) if path.exists() else {}
items[key] = value
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(items, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

require_value() {
  local name="$1"
  local value="${!name}"
  if [ -z "$value" ]; then
    echo "缺少必要參數：${name}。請以參數或 .local/up.env 提供。"
    exit 1
  fi
}

validate_source_cidr() {
  local cidr="$1"
  if [[ "$cidr" == "0.0.0.0/0" || "$cidr" == "::/0" || "$cidr" == "0.0.0.0/32" ]]; then
    echo "SOURCE_CIDR 不能為 0.0.0.0/0、::/0 或 0.0.0.0/32。必須為單一 IPv4 /32。"
    exit 1
  fi

  if [[ ! "$cidr" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]]; then
    echo "SOURCE_CIDR 必須是單一 IPv4 /32，例如 203.0.113.5/32。"
    exit 1
  fi

  local octet
  IFS='.' read -r -a octets <<< "${cidr%/32}"
  for octet in "${octets[@]}"; do
    if ! [[ "$octet" =~ ^[0-9]+$ ]] || [ "$octet" -lt 0 ] || [ "$octet" -gt 255 ]; then
      echo "SOURCE_CIDR 含有無效 IPv4 octet：$cidr"
      exit 1
    fi
  done
}

ensure_aws_ready() {
  python3 "$ROOT_DIR/scripts/lab.py" verify >/dev/null
}

load_resource_plan() {
  local plan_json
  plan_json="$(python3 - "$RESOURCES_FILE" <<'PY'
import json, re, sys
from pathlib import Path

path = Path(sys.argv[1])
if path.exists():
  try:
    items = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Cannot safely read resources.json ({type(exc).__name__}); refusing deployment")
else:
  items = {}

if not isinstance(items, dict):
  raise SystemExit("resources.json must contain a JSON object; refusing deployment")

allowed = {"security_group_id", "key_pair_name", "instance_id"}
unknown = [key for key, value in items.items() if key not in allowed and value not in (None, "")]
if unknown:
  raise SystemExit("resources.json contains unsupported nonempty fields: " + ", ".join(unknown))

def manifest_string(key):
  value = items.get(key, "")
  if value is None or value == "":
    return ""
  if not isinstance(value, str):
    raise SystemExit(f"resources.json {key} must be a string; refusing deployment")
  return value

security_group_id = manifest_string("security_group_id")
key_pair_name = manifest_string("key_pair_name")
instance_id = manifest_string("instance_id")
if instance_id:
  raise SystemExit("resources.json already records an instance; refusing to create another")
if security_group_id and not re.fullmatch(r"sg-[a-z0-9]+", str(security_group_id)):
  raise SystemExit("resources.json security_group_id is invalid; refusing deployment")
if key_pair_name and not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,128}", str(key_pair_name)):
  raise SystemExit("resources.json key_pair_name is invalid; refusing deployment")

if not security_group_id and not key_pair_name:
  mode = "fresh"
elif security_group_id and key_pair_name:
  mode = "resume"
else:
  raise SystemExit("resources.json contains an incomplete resource pair; refusing deployment")

print(json.dumps({"mode": mode, "security_group_id": security_group_id, "key_pair_name": key_pair_name}))
PY
  )"

  RESUME_EXISTING_RESOURCES="$(python3 -c 'import json,sys; print("true" if json.loads(sys.argv[1])["mode"] == "resume" else "false")' "$plan_json")"
  RESUMED_SECURITY_GROUP_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["security_group_id"])' "$plan_json")"
  RESUMED_KEY_PAIR_NAME="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["key_pair_name"])' "$plan_json")"
}

verify_resumed_security_group() {
  local group_json
  group_json="$(aws_cmd ec2 describe-security-groups --group-ids "$RESUMED_SECURITY_GROUP_ID")"

  python3 - "$group_json" "$RESUMED_SECURITY_GROUP_ID" "$GROUP_NAME" "$VPC_ID" "$SOURCE_CIDR" "$OWNER" <<'PY'
import json, sys

result = json.loads(sys.argv[1])
security_group_id, group_name, vpc_id, source_cidr, owner = sys.argv[2:]
groups = result.get("SecurityGroups", [])
if len(groups) != 1 or groups[0].get("GroupId") != security_group_id:
    raise SystemExit("Exact manifest security group ID did not resolve uniquely")
group = groups[0]
if group.get("GroupName") != f"{group_name}-sg" or group.get("VpcId") != vpc_id:
    raise SystemExit("Existing SG GroupName/VpcId do not match deployment configuration")

expected = {(22, 22, source_cidr), (80, 80, source_cidr)}
observed = []
for permission in group.get("IpPermissions", []):
    if permission.get("IpProtocol") != "tcp":
        raise SystemExit("Existing SG has unexpected non-TCP ingress")
    if permission.get("Ipv6Ranges") or permission.get("PrefixListIds") or permission.get("UserIdGroupPairs"):
        raise SystemExit("Existing SG has unexpected non-IPv4 ingress source")
    ranges = permission.get("IpRanges", [])
    if len(ranges) != 1 or permission.get("FromPort") != permission.get("ToPort"):
        raise SystemExit("Existing SG ingress rule shape is unexpected")
    observed.append((permission.get("FromPort"), permission.get("ToPort"), ranges[0].get("CidrIp")))
if sorted(observed) != sorted(expected):
    raise SystemExit("Existing SG ingress does not exactly match TCP 22/80 from SOURCE_CIDR")

tags = {tag.get("Key"): tag.get("Value") for tag in group.get("Tags") or []}
if tags:
    required = {"course": "yuntech-115-1", "week": "w03", "group": group_name, "owner": owner}
    if any(tags.get(key) != value for key, value in required.items()):
        raise SystemExit("Existing SG tags do not match this course/group/owner")
else:
    print("Existing SG has no tags; exact manifest ID plus matching VPC/name/ingress will be used for resume verification.", file=sys.stderr)
PY

  local eni_json
  eni_json="$(aws_cmd ec2 describe-network-interfaces --filters "Name=group-id,Values=${RESUMED_SECURITY_GROUP_ID}")"
  python3 -c 'import json,sys; data=json.loads(sys.argv[1]); n=data.get("NetworkInterfaces", []); (sys.exit("Existing SG is attached to ENIs; refusing resume") if n else None)' "$eni_json"

  local instance_json
  instance_json="$(aws_cmd ec2 describe-instances --filters "Name=instance.group-id,Values=${RESUMED_SECURITY_GROUP_ID}")"
  python3 -c 'import json,sys; data=json.loads(sys.argv[1]); n=[i for r in data.get("Reservations", []) for i in r.get("Instances", [])]; (sys.exit("Existing SG is associated with EC2; refusing resume") if n else None)' "$instance_json"
}

verify_resumed_key_pair() {
  local expected_key_name="${KEY_NAME:-${GROUP_NAME}-w03-key}"
  if [ "$RESUMED_KEY_PAIR_NAME" != "$expected_key_name" ]; then
    echo "resources.json key_pair_name does not match the configured key name; refusing resume." >&2
    return 1
  fi

  local private_key_path="$HOME/.ssh/${RESUMED_KEY_PAIR_NAME}"
  local public_key_path="$HOME/.ssh/${RESUMED_KEY_PAIR_NAME}.pub"
  if [ ! -f "$private_key_path" ] || [ ! -f "$public_key_path" ]; then
    echo "Local key-pair files are incomplete; refusing resume." >&2
    return 1
  fi
  local private_key_mode
  private_key_mode="$(stat -c '%a' "$private_key_path")"
  if [ "$private_key_mode" != "600" ]; then
    echo "Local private key permissions must be 600; refusing resume." >&2
    return 1
  fi

  local local_fingerprint
  local_fingerprint="$(ssh-keygen -E sha256 -lf "$public_key_path" | awk 'NR == 1 { sub(/^SHA256:/, "", $2); sub(/=+$/, "", $2); print $2 }')"
  if [ -z "$local_fingerprint" ]; then
    echo "Could not calculate local public-key fingerprint; refusing resume." >&2
    return 1
  fi

  local key_pair_json
  key_pair_json="$(aws_cmd ec2 describe-key-pairs --key-names "$RESUMED_KEY_PAIR_NAME")"
  RESUMED_KEY_PAIR_ID="$(python3 - "$key_pair_json" "$RESUMED_KEY_PAIR_NAME" "$local_fingerprint" "$GROUP_NAME" "$OWNER" <<'PY'
import json, re, sys

result = json.loads(sys.argv[1])
key_name, local_fingerprint, group_name, owner = sys.argv[2:]
pairs = result.get("KeyPairs", [])
if len(pairs) != 1 or pairs[0].get("KeyName") != key_name:
    raise SystemExit("Exact key-pair name did not resolve uniquely")
pair = pairs[0]
if pair.get("KeyType", "").lower() != "ed25519":
    raise SystemExit("Existing AWS key pair is not ed25519")
aws_fingerprint = str(pair.get("KeyFingerprint", "")).removeprefix("SHA256:").rstrip("=")
if not aws_fingerprint or aws_fingerprint != local_fingerprint:
    raise SystemExit("AWS key-pair fingerprint does not match the local public key")
key_pair_id = pair.get("KeyPairId", "")
if not re.fullmatch(r"key-[a-z0-9]+", key_pair_id):
    raise SystemExit("AWS did not return a valid key-pair ID")
tags = {tag.get("Key"): tag.get("Value") for tag in pair.get("Tags") or []}
if tags:
    required = {"course": "yuntech-115-1", "week": "w03", "group": group_name, "owner": owner}
    if any(tags.get(key) != value for key, value in required.items()):
        raise SystemExit("Existing key-pair tags do not match this course/group/owner")
print(key_pair_id)
PY
  )"

  IMPORTED_KEY_PAIR_NAME="$RESUMED_KEY_PAIR_NAME"
}

prepare_resource_state() {
  load_resource_plan
  if [ "$RESUME_EXISTING_RESOURCES" = true ]; then
    verify_resumed_security_group
    verify_resumed_key_pair
    echo "已唯讀驗證 manifest 中既有 SG 與 key pair；續跑時不會重新建立或匯入。"
  else
    echo "resources.json 為空，使用全新部署流程。"
  fi
}

resolve_ami() {
  if [ -z "$AMI_ID" ]; then
    AMI_ID="$(aws_cmd ssm get-parameters --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query 'Parameters[0].Value' --output text)"
  fi

  local architecture
  architecture="$(aws_cmd ec2 describe-images --image-ids "$AMI_ID" --owners amazon --query 'Images[0].Architecture' --output text)"
  if [ "$architecture" != "x86_64" ]; then
    echo "AL2023 AMI 不是 x86_64：${AMI_ID} (${architecture})"
    exit 1
  fi

  local ami_name
  ami_name="$(aws_cmd ec2 describe-images --image-ids "$AMI_ID" --owners amazon --query 'Images[0].Name' --output text)"
  local creation_date
  creation_date="$(aws_cmd ec2 describe-images --image-ids "$AMI_ID" --owners amazon --query 'Images[0].CreationDate' --output text)"
  echo "AMI_ID=${AMI_ID}"
  echo "AMI_NAME=${ami_name}"
  echo "AMI_CREATION_DATE=${creation_date}"
}

create_security_group() {
  local group_name="$1"
  local vpc_id="$2"
  local sg_id

  if ! sg_id="$(aws_cmd ec2 create-security-group \
    --group-name "$group_name" \
    --description "W3 inspection prototype for ${GROUP_NAME} / ${OWNER}" \
    --vpc-id "$vpc_id" \
    --query 'GroupId' --output text)"; then
    echo "create-security-group 失敗，未取得 SG ID。" >&2
    return 1
  fi

  if [[ ! "$sg_id" =~ ^sg-[a-z0-9]+$ ]]; then
    echo "create-security-group 未回傳有效 SG ID：${sg_id}"
    return 1
  fi

  write_resources_json security_group_id "$sg_id"
  CREATED_SECURITY_GROUP_ID="$sg_id"

  if ! aws_cmd ec2 authorize-security-group-ingress \
    --group-id "$sg_id" \
    --ip-permissions "[{\"IpProtocol\":\"tcp\",\"FromPort\":22,\"ToPort\":22,\"IpRanges\":[{\"CidrIp\":\"${SOURCE_CIDR}\"}]},{\"IpProtocol\":\"tcp\",\"FromPort\":80,\"ToPort\":80,\"IpRanges\":[{\"CidrIp\":\"${SOURCE_CIDR}\"}]}]" >/dev/null; then
    echo "authorize-security-group-ingress 失敗，但 SG ID 已先寫入 resources.json：${sg_id}" >&2
    return 1
  fi
}

ensure_key_pair() {
  local key_name="$1"
  local key_path="$HOME/.ssh/${key_name}"
  IMPORTED_KEY_PAIR_NAME=""
  mkdir -p "$HOME/.ssh"
  chmod 700 "$HOME/.ssh"

  if [ ! -f "$key_path" ]; then
    if ! ssh-keygen -t ed25519 -f "$key_path" -N "" -C "${GROUP_NAME}@${OWNER}" >&2; then
      echo "ssh-keygen 失敗；未匯入 key pair。" >&2
      return 1
    fi
  fi
  if [ ! -f "${key_path}.pub" ]; then
    echo "找不到公鑰檔 ${key_path}.pub；未匯入 key pair。" >&2
    return 1
  fi
  chmod 600 "$key_path"

  if ! aws_cmd ec2 import-key-pair \
    --key-name "$key_name" \
    --public-key-material "fileb://${key_path}.pub" >/dev/null
  then
    echo "import-key-pair 失敗；未記錄 key_pair_name。" >&2
    return 1
  fi

  IMPORTED_KEY_PAIR_NAME="$key_name"
}

build_user_data() {
  local commit="$1"
  local output_file="$2"
  bash "$ROOT_DIR/deploy/make-user-data.sh" "$commit" "$output_file"
}

create_instance() {
  local key_name="$1"
  local security_group_id="$2"
  local subnet_id="$3"
  local ami_id="$4"
  local user_data_file="$5"

  local instance_id
  if ! instance_id="$(aws_cmd ec2 run-instances \
    --image-id "$ami_id" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$key_name" \
    --security-group-ids "$security_group_id" \
    --subnet-id "$subnet_id" \
    --user-data "file://${user_data_file}" \
    --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
    --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":8,\"VolumeType\":\"gp3\",\"Encrypted\":true,\"DeleteOnTermination\":true}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=${GROUP_NAME}},{Key=owner,Value=${OWNER}}]" "ResourceType=volume,Tags=[{Key=course,Value=yuntech-115-1},{Key=week,Value=w03},{Key=group,Value=${GROUP_NAME}},{Key=owner,Value=${OWNER}}]" \
    --query 'Instances[0].InstanceId' --output text)"; then
    echo "run-instances 失敗；沒有取得 instance ID，請保留 resources.json 並先查明 AWS 結果，不要直接重跑。" >&2
    return 1
  fi

  if [[ ! "$instance_id" =~ ^i-[a-z0-9]+$ ]]; then
    echo "run-instances 未回傳純 instance ID：${instance_id}；停止且不寫入空白或錯誤 ID。" >&2
    return 1
  fi

  echo "$instance_id"
}

ssh_instance() {
  local key_name="$1"
  local public_ip="$2"
  local command="$3"

  ssh -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$HOME/.ssh/known_hosts" \
    -o BatchMode=yes \
    -i "$HOME/.ssh/${key_name}" \
    "ec2-user@${public_ip}" \
    "$command"
}

verify_ssh_host_key() {
  local public_ip="$1"
  local candidate_file="$ROOT_DIR/.local/host-key-candidate"
  local known_hosts_file="$HOME/.ssh/known_hosts"
  local scan_output
  local candidate_line
  local candidate_key
  local candidate_fingerprint
  local confirmed_fingerprint
  local known_hosts_key

  if ! scan_output="$(ssh-keyscan -T 8 -t ed25519 "$public_ip" 2>/dev/null)"; then
    echo "無法取得 SSH host-key 候選資料；停止，不進行 SSH。" >&2
    return 1
  fi

  candidate_line="$(awk '$2 == "ssh-ed25519" { print; exit }' <<<"$scan_output")"
  if [ -z "$candidate_line" ]; then
    echo "沒有取得 ed25519 host-key 候選資料；停止，不進行 SSH。" >&2
    return 1
  fi

  mkdir -p "$ROOT_DIR/.local"
  printf '%s\n' "$candidate_line" > "$candidate_file"
  candidate_key="$(awk '{ print $2, $3 }' <<<"$candidate_line")"
  candidate_fingerprint="$(ssh-keygen -lf <(printf '%s\n' "$candidate_key") | awk 'NR == 1 { print $2 }')"

  echo "=== 首次 SSH host-key 人工核對 ==="
  echo "EC2 Public IP: ${public_ip}"
  echo "候選 fingerprint（僅供比對，ssh-keyscan 不代表可信）：${candidate_fingerprint}"
  echo "請先從可信且獨立於此 SSH 連線的來源取得預期 fingerprint，並由你本人核對。"
  echo "不確定或無法核對時請取消；不可只因候選值來自 ssh-keyscan 就信任。"
  read -r -p "核對相符後，請輸入可信來源上的完整 fingerprint：" confirmed_fingerprint
  if [ "$confirmed_fingerprint" != "$candidate_fingerprint" ]; then
    echo "Fingerprint 不符；停止，不進行 SSH。" >&2
    return 1
  fi

  echo "已核對候選值。請在另一個 terminal 手動執行以下命令，將同一筆候選 key 加入 known_hosts："
  printf 'install -d -m 700 %q\n' "$HOME/.ssh"
  printf 'touch %q\n' "$known_hosts_file"
  printf '%s\n' "awk '\$2 == \"ssh-ed25519\" { print; exit }' \"$candidate_file\" >> \"$known_hosts_file\""
  printf 'chmod 600 %q\n' "$known_hosts_file"
  read -r -p "完成手動登錄後按 Enter，腳本會確認 known_hosts 內容才繼續：" _

  if [ ! -f "$known_hosts_file" ]; then
    echo "找不到 $known_hosts_file；停止，不進行 SSH。" >&2
    return 1
  fi

  known_hosts_key="$(ssh-keygen -F "$public_ip" -f "$known_hosts_file" 2>/dev/null | awk '$2 == "ssh-ed25519" { print $2, $3; exit }')"
  if [ "$known_hosts_key" != "$candidate_key" ]; then
    echo "known_hosts 中該 IP 的 key 與人工核對的候選 key 不一致；停止，不進行 SSH。若為舊 IP 記錄，請先核對後再手動處理。" >&2
    return 1
  fi

  echo "known_hosts 已包含核對過的 host key；StrictHostKeyChecking=yes 將保留啟用。"
}

record_early_curl() {
  local instance_id="$1"
  local public_ip
  public_ip="$(aws_cmd ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"

  if [ -z "$public_ip" ] || [ "$public_ip" = "None" ]; then
    echo "EC2 尚未取得 public IP，尚無法執行 early curl。"
    return 0
  fi

  local early_dir="$ROOT_DIR/.local"
  local early_http_file="$early_dir/early-curl.txt"
  local early_headers_file="$early_dir/early-curl-headers.txt"
  local early_body_file="$early_dir/early-curl-body.txt"
  local early_result

  early_result="$(curl --silent --show-error --location --connect-timeout 8 --max-time 20 \
    -D "$early_headers_file" -o "$early_body_file" \
    -w 'http_code=%{http_code}\nexit_code=%{exitcode}\n' "http://${public_ip}/health" 2>&1 || true)"

  printf '%s\n' "$early_result" > "$early_http_file"
  write_observation "early_curl" "$early_result"
  write_observation "early_curl_time" "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}

observe_cloud_init_complete() {
  local key_name="$1"
  local public_ip="$2"
  local output
  local started_at
  local completed_at
  local command_status

  started_at="$(utc_now)"
  write_observation "cloud_init_started_at" "$started_at"
  if output="$(ssh_instance "$key_name" "$public_ip" 'sudo cloud-init status --wait' 2>&1)"; then
    command_status=0
  else
    command_status=$?
  fi
  completed_at="$(utc_now)"
  write_observation "cloud_init_completed_at" "$completed_at"
  write_observation "cloud_init_exit_code" "$command_status"
  write_observation "cloud_init_status" "$output"

  if [ "$command_status" -ne 0 ] || [[ "$output" != *"status: done"* ]]; then
    echo "cloud-init 未完成：${output}"
    return 1
  fi

  write_observation "cloud_init_complete" "$completed_at"
}

observe_nginx_and_inspection() {
  local key_name="$1"
  local public_ip="$2"
  local output
  local nginx_listener
  local inspection_listener

  if ! output="$(ssh_instance "$key_name" "$public_ip" 'sudo ss -lntH' 2>&1)"; then
    echo "無法讀取 nginx / inspection 監聽資訊：${output}" >&2
    return 1
  fi

  nginx_listener="$(awk '$1 == "LISTEN" && $4 ~ /:80$/ { print $4; exit }' <<<"$output")"
  inspection_listener="$(awk '$1 == "LISTEN" && $4 == "127.0.0.1:8080" { print $4; exit }' <<<"$output")"
  if [ -z "$nginx_listener" ] || [ -z "$inspection_listener" ]; then
    echo "nginx / inspection 監聽未就緒；預期 :80 與 127.0.0.1:8080，實際：${output}" >&2
    return 1
  fi

  write_observation "nginx_listener" "$nginx_listener"
  write_observation "inspection_listener" "$inspection_listener"
  write_observation "nginx_inspection_status" "$output"
  write_observation "nginx_inspection_listening" "$(utc_now)"
}

validate_instance_ready() {
  local instance_id="$1"
  local key_name="${KEY_NAME:-${GROUP_NAME}-w03-key}"
  local instance_state
  local check_states
  local instance_check
  local system_check

  aws_cmd ec2 wait instance-running --instance-ids "$instance_id"
  instance_state="$(aws_cmd ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  if [ "$instance_state" != "running" ]; then
    echo "EC2 未讀回 running 狀態：${instance_state}" >&2
    return 1
  fi
  write_observation "instance_running_state" "$instance_state"
  write_observation "instance_running" "$(utc_now)"

  record_early_curl "$instance_id"

  aws_cmd ec2 wait instance-status-ok --instance-ids "$instance_id"
  check_states="$(aws_cmd ec2 describe-instance-status --instance-ids "$instance_id" --query 'InstanceStatuses[0].[InstanceStatus.Status,SystemStatus.Status]' --output text)"
  read -r instance_check system_check <<<"$check_states"
  if [ "$instance_check" != "ok" ] || [ "$system_check" != "ok" ]; then
    echo "EC2 2/2 status checks 未通過：instance=${instance_check:-missing}, system=${system_check:-missing}" >&2
    return 1
  fi
  write_observation "status_checks_instance" "$instance_check"
  write_observation "status_checks_system" "$system_check"
  write_observation "status_checks_ok" "$(utc_now)"

  local public_ip
  public_ip="$(aws_cmd ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
  if [ -z "$public_ip" ] || [ "$public_ip" = "None" ]; then
    echo "EC2 未取得 public IP，原因可能是 subnet 非 public 或未設定 public 路由。"
    exit 1
  fi

  verify_ssh_host_key "$public_ip"
  observe_cloud_init_complete "$key_name" "$public_ip"
  observe_nginx_and_inspection "$key_name" "$public_ip"

  local health_url="http://${public_ip}/health"
  local health_json_file="$ROOT_DIR/.local/health.json"
  local http_status
  local curl_exit_code
  if http_status="$(curl --silent --show-error --location --connect-timeout 8 --max-time 20 -D "$ROOT_DIR/.local/health-headers.txt" \
    -o "$health_json_file" -w '%{http_code}' "$health_url")"; then
    curl_exit_code=0
  else
    curl_exit_code=$?
  fi
  write_observation "health_curl_exit_code" "$curl_exit_code"
  write_observation "health_http_status" "$http_status"
  if [ "$curl_exit_code" -ne 0 ] || [ "$http_status" != "200" ]; then
  # 這裡是其他的程式碼...
    return 1
  fi

  local version
  if ! version="$(python3 - "$health_json_file" "$DEPLOY_COMMIT" <<'PY'
import json, re, sys
from datetime import datetime, timedelta

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)

if not isinstance(payload, dict):
    raise SystemExit("health response must be a JSON object")
if payload.get("status") != "ok" or payload.get("service") != "inspection":
    raise SystemExit("health status/service contract mismatch")
version = payload.get("version", "")
if not re.fullmatch(r"[0-9a-f]{40}", version) or version != sys.argv[2]:
    raise SystemExit("health version does not match the 40-character deployment commit")
started_at = payload.get("started_at", "")
try:
    timestamp = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
except (TypeError, ValueError):
    raise SystemExit("health started_at is not an ISO timestamp")
if timestamp.utcoffset() != timedelta(0):
    raise SystemExit("health started_at is not UTC")
print(version)
PY
)"; then
    echo "健康頁 JSON 不符合成功契約，請檢查 .local/health.json。" >&2
    return 1
  fi

  local health_observed_at
  health_observed_at="$(utc_now)"
  write_observation "health_200" "$health_observed_at"
  write_observation "health_200_time" "$health_observed_at"
  write_observation "health_version" "$version"
  write_observation "deployment_commit" "$DEPLOY_COMMIT"
  write_observation "git_commit_time" "$(git -C "$ROOT_DIR" show -s --format='%cI' "$DEPLOY_COMMIT")"
}

main() {
  echo "=== 檢查 AWS 環境 ==="
  ensure_aws_ready

  require_value SOURCE_CIDR
  require_value GROUP_NAME
  require_value OWNER
  require_value DEPLOY_COMMIT
  require_value VPC_ID
  require_value SUBNET_ID

  validate_source_cidr "$SOURCE_CIDR"
  prepare_resource_state
  resolve_ami

  echo "=== 即將建立的資源清單 ==="
  echo "- VPC: ${VPC_ID}"
  echo "- Subnet: ${SUBNET_ID}"
  echo "- AMI: ${AMI_ID}"
  if [ "$RESUME_EXISTING_RESOURCES" = true ]; then
    echo "- Reuse existing SG: ${RESUMED_SECURITY_GROUP_ID} (${GROUP_NAME}-sg); ingress TCP 22/80 from ${SOURCE_CIDR}"
    echo "- Reuse existing key pair: ${RESUMED_KEY_PAIR_NAME} (${RESUMED_KEY_PAIR_ID})"
  else
    echo "- Create SG: ${GROUP_NAME}-sg"
    echo "- Import key pair: ${KEY_NAME:-${GROUP_NAME}-w03-key}"
  fi
  echo "- Create exactly one EC2: ${INSTANCE_TYPE}, AL2023 x86_64, deployment commit ${DEPLOY_COMMIT}"
  echo "- Root EBS: 8 GiB, encrypted gp3, DeleteOnTermination=true"
  echo "- Metadata: IMDSv2 required"
  echo "- Network exposure: TCP 22/80 limited to ${SOURCE_CIDR}; inspection listens only on 127.0.0.1:8080"
  echo "- Cost categories: running t3.micro compute, gp3 root volume, public IPv4; current rates depend on region and AWS pricing"
  echo "- Source /32: ${SOURCE_CIDR}"
  echo "- User data: ${ROOT_DIR}/.local/w03-user-data.sh"
  echo ""
  echo "請人工確認上述清單，確認後輸入: yes"
  read -r response
  if [ "$response" != "yes" ]; then
    echo "已取消，未建立任何 AWS 資源。"
    exit 0
  fi

  mkdir -p "$ROOT_DIR/.local"

  local sg_name="${GROUP_NAME}-sg"
  local key_name="${KEY_NAME:-${GROUP_NAME}-w03-key}"
  local user_data_file="${ROOT_DIR}/.local/w03-user-data.sh"

  local imported_key_name
  local sg_id
  if [ "$RESUME_EXISTING_RESOURCES" = true ]; then
    sg_id="$RESUMED_SECURITY_GROUP_ID"
    imported_key_name="$RESUMED_KEY_PAIR_NAME"
  else
    create_security_group "$sg_name" "$VPC_ID"
    sg_id="$CREATED_SECURITY_GROUP_ID"

    ensure_key_pair "$key_name"
    imported_key_name="$IMPORTED_KEY_PAIR_NAME"
    write_resources_json key_pair_name "$imported_key_name"
  fi

  build_user_data "$DEPLOY_COMMIT" "$user_data_file"

  local instance_id
  instance_id="$(create_instance "$imported_key_name" "$sg_id" "$SUBNET_ID" "$AMI_ID" "$user_data_file")"
  write_resources_json instance_id "$instance_id"

  echo "=== 建立完成，開始記錄 T2 五層首次觀測時間與 early curl ==="
  validate_instance_ready "$instance_id"

  echo "=== T2 必要資訊 ==="
  echo "Instance ID: $instance_id"
  echo "Deployment commit: $DEPLOY_COMMIT"
  echo "Public IPv4: $(aws_cmd ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
  echo "Health URL: http://$(aws_cmd ec2 describe-instances --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)/health"
  echo "Commit time: $(git -C "$ROOT_DIR" show -s --format='%cI' "$DEPLOY_COMMIT")"
  echo "Observations file: $OBSERVATIONS_FILE"

  echo "T2 準備階段完成。"
}

main "$@"
