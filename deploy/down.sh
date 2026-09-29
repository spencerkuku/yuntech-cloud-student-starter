#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${CONFIG_FILE:-${ROOT_DIR}/.local/up.env}"

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

exec 3<&0
exec python3 - "$ROOT_DIR" "$AWS_REGION" "$SOURCE_CIDR" "$GROUP_NAME" "$OWNER" "$@" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
region, source_cidr, group_name, owner = sys.argv[2:6]
arguments = sys.argv[6:]
resources_path = root_dir / ".local" / "resources.json"

sys.path.insert(0, str(root_dir / "scripts"))
import lab


def fail(message):
    print(f"STOP: {message}", file=sys.stderr)
    raise SystemExit(1)


def aws(args):
    return lab.run_aws(args, region, env=lab.clean_env(region))


def load_resources():
    try:
        resources = json.loads(resources_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot safely read resources.json ({type(exc).__name__})")
    if not isinstance(resources, dict):
        fail("resources.json must contain a JSON object")
    return resources


def check_tags(resource, expected):
    tags = {tag.get("Key"): tag.get("Value") for tag in resource.get("Tags") or []}
    if not tags:
        return False
    mismatches = [key for key, value in expected.items() if tags.get(key) != value]
    if mismatches:
        fail("resource tags do not prove course ownership: " + ", ".join(mismatches))
    return True


def get_instance(resources, expected_tags):
    instance_id = resources.get("instance_id", "")
    if instance_id in (None, ""):
        print("instance_id 為空；略過 instance stop/terminate。")
        return None
    if not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]+", instance_id):
        fail("instance_id is non-empty but invalid; refusing to guess or search by name")

    result = aws(["ec2", "describe-instances", "--instance-ids", instance_id])
    instances = [item for reservation in result.get("Reservations", []) for item in reservation.get("Instances", [])]
    if len(instances) != 1 or instances[0].get("InstanceId") != instance_id:
        fail("the exact instance ID did not resolve to exactly one instance")
    if not check_tags(instances[0], expected_tags):
        fail("instance has no ownership tags; refusing stop")
    return instances[0]


def stop_instance(instance, instance_id, expected_tags):
    if instance is None:
        print("resources.json 沒有 instance ID；沒有 AWS 資源需要停止。")
        return
    state = instance.get("State", {}).get("Name")
    if state == "stopped":
        print(f"Instance {instance_id} 已是 stopped；未執行變更。")
        return
    if state != "running":
        fail(f"instance state is {state!r}; refusing stop")
    print("即將停止的精確資源：")
    print(f"- EC2 instance: {instance_id} ({state})")
    confirm(f"STOP {instance_id}")
    aws(["ec2", "stop-instances", "--instance-ids", instance_id])
    aws(["ec2", "wait", "instance-stopped", "--instance-ids", instance_id])
    after = get_instance({"instance_id": instance_id}, expected_tags)
    if after.get("State", {}).get("Name") != "stopped":
        fail("instance did not read back as stopped")
    print(f"已讀回 instance {instance_id} state=stopped。")


def confirm(token):
    print(f"輸入 {token!r} 才繼續：", end="", flush=True)
    try:
        with os.fdopen(os.dup(3), "r", encoding="utf-8") as confirmation_input:
            answer = confirmation_input.readline().strip()
    except OSError:
        fail("interactive input is unavailable; no changes made")
    if answer != token:
        fail("confirmation did not match; no changes made")


def sg_rules_match(group):
    observed = []
    for permission in group.get("IpPermissions", []):
        if permission.get("IpProtocol") != "tcp":
            return False
        if permission.get("Ipv6Ranges") or permission.get("PrefixListIds") or permission.get("UserIdGroupPairs"):
            return False
        ranges = permission.get("IpRanges", [])
        if len(ranges) != 1:
            return False
        observed.append((permission.get("FromPort"), permission.get("ToPort"), ranges[0].get("CidrIp")))
    return sorted(observed) == sorted([(22, 22, source_cidr), (80, 80, source_cidr)])


def verify_sg_absent(security_group_id):
    try:
        result = aws(["ec2", "describe-security-groups", "--group-ids", security_group_id])
    except lab.LabError as exc:
        if "InvalidGroup.NotFound" in str(exc):
            return True
        raise
    return not result.get("SecurityGroups", [])


def main():
    if arguments not in ([], ["--stop"]):
        fail("usage: deploy/down.sh [--stop]")
    if region not in ("us-east-1", "us-west-2"):
        fail("region is outside the course-approved regions")

    resources = load_resources()
    expected_tags = {
        "course": "yuntech-115-1",
        "week": "w03",
        "group": group_name,
        "owner": owner,
    }
    instance_id = resources.get("instance_id", "")
    if instance_id not in (None, "") and (
        not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]+", instance_id)
    ):
        fail("instance_id is non-empty but invalid; refusing to guess or search by name")
    if arguments == ["--stop"]:
        ctx = lab.verify()
        if ctx["region"] != region:
            fail("configured region does not match verified Learner Lab context")
        instance = get_instance(resources, expected_tags)
        stop_instance(instance, instance_id, expected_tags)
        return

    if instance_id not in (None, ""):
        fail("this partial-cleanup path is SG-only; reconcile the recorded instance ID separately")

    key_pair_name = resources.get("key_pair_name", "")
    valid_key_name = isinstance(key_pair_name, str) and bool(
        re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,128}", key_pair_name)
    )
    if key_pair_name not in (None, "") and not valid_key_name:
        print("key_pair_name 格式無效／含污染內容；略過，不會以該值查詢或刪除任何 key pair。")
    elif valid_key_name:
        fail("a non-empty valid key_pair_name is present; this SG-only partial cleanup will not process it")

    security_group_id = resources.get("security_group_id", "")
    if not isinstance(security_group_id, str) or not re.fullmatch(r"sg-[0-9a-f]+", security_group_id):
        fail("resources.json has no valid exact security_group_id")
    if not source_cidr or not group_name or not owner:
        fail("SOURCE_CIDR, GROUP_NAME, and OWNER must be provided by .local/up.env")

    ctx = lab.verify()
    if ctx["region"] != region:
        fail("configured region does not match verified Learner Lab context")

    result = aws(["ec2", "describe-security-groups", "--group-ids", security_group_id])
    groups = result.get("SecurityGroups", [])
    if len(groups) != 1 or groups[0].get("GroupId") != security_group_id:
        fail("the exact SG ID did not resolve to exactly one security group")
    group = groups[0]

    expected_group_name = f"{group_name}-sg"
    expected_vpc_id = os.environ.get("VPC_ID", "")
    if group.get("GroupName") != expected_group_name or not expected_vpc_id or group.get("VpcId") != expected_vpc_id:
        fail("SG GroupName/VpcId do not match the local deployment configuration")

    has_owner_tags = check_tags(group, expected_tags)
    if not has_owner_tags and not sg_rules_match(group):
        fail("untagged SG rules do not exactly match this group's planned TCP 22/80 /32 ingress")

    eni_result = aws([
        "ec2", "describe-network-interfaces", "--filters",
        f"Name=group-id,Values={security_group_id}",
    ])
    interfaces = eni_result.get("NetworkInterfaces", [])
    if interfaces:
        fail("SG is attached to one or more ENIs; refusing deletion")

    instance_result = aws([
        "ec2", "describe-instances", "--filters",
        f"Name=instance.group-id,Values={security_group_id}",
    ])
    instances = [item for reservation in instance_result.get("Reservations", []) for item in reservation.get("Instances", [])]
    if instances:
        fail("SG is associated with one or more EC2 instances; refusing deletion")

    print("=== 即將刪除的精確資源清單 ===")
    print(f"- Security Group ID: {security_group_id}")
    print(f"  GroupName: {group['GroupName']}; VPC: {group['VpcId']}")
    print(f"  Ownership evidence: {'matching course tags' if has_owner_tags else 'exact manifest ID + matching GroupName/VPC/ingress; SG has no tags'}")
    print(f"  Inbound rules: TCP 22 and 80 from {source_cidr}")
    print("  Direct SG charge: none; current ENI and EC2 association checks are empty")
    print("  Network effect: removes the listed TCP 22/80 inbound access")
    print("  Recovery: deleted SG ID cannot be restored; replacement would be a new resource requiring separate approval")
    print("- EC2 instance: none recorded; empty instance_id ignored")
    print("- Key pair: polluted manifest value ignored; no name-based query or deletion")
    print("- ENIs using SG: none")
    print("- EC2 instances using SG: none")
    if not has_owner_tags:
        print("WARNING: SG has no ownership tags. Confirm its creation evidence and exact ID before approving.")
    confirm(f"DELETE {security_group_id}")

    aws(["ec2", "delete-security-group", "--group-id", security_group_id])
    if not verify_sg_absent(security_group_id):
        fail("delete request returned but SG still exists on read-back")
    print(f"Read-back verified: Security Group {security_group_id} does not exist.")


if __name__ == "__main__":
    try:
        main()
    except lab.LabError as exc:
        fail(str(exc))
PY