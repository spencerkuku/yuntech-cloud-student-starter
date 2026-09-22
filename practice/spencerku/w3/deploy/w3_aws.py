#!/usr/bin/env python3
"""Create, stop, or remove only the W3 resources recorded locally."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[4]
LOCAL = ROOT / "practice" / "spencerku" / "w3" / ".local"
CONFIG = LOCAL / "w3.env"
RESOURCES = LOCAL / "resources.json"
sys.path.insert(0, str(ROOT / "scripts"))
import lab  # noqa: E402


def fail(message):
    raise SystemExit(f"STOP: {message}")


def load_config(require_public_key=True):
    if not CONFIG.exists():
        fail(f"missing {CONFIG}; copy w3.env.example and fill verified values")
    values = {}
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            fail(f"invalid config line: {line}")
        key, value = line.split("=", 1)
        values[key.strip()] = os.path.expandvars(value.strip().strip('"').strip("'"))
    required = ("AWS_REGION", "VPC_ID", "SUBNET_ID", "AMI_ID", "SOURCE_CIDR",
                "GROUP_NAME", "OWNER_CODE", "KEY_NAME", "PUBLIC_KEY_FILE")
    missing = [key for key in required if not values.get(key)]
    if missing:
        fail("missing config values: " + ", ".join(missing))
    if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}/32", values["SOURCE_CIDR"]):
        fail("SOURCE_CIDR must be one IPv4 address with /32")
    if values["VPC_ID"].startswith("vpc-replace") or values["AMI_ID"].startswith("ami-replace"):
        fail("replace placeholder VPC_ID, SUBNET_ID, and AMI_ID with T1-verified values")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,30}", values["GROUP_NAME"]):
        fail("GROUP_NAME contains unsupported characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,30}", values["OWNER_CODE"]):
        fail("OWNER_CODE contains unsupported characters")
    public_key = Path(os.path.expanduser(values["PUBLIC_KEY_FILE"]))
    if require_public_key:
        if not public_key.is_file():
            fail(f"public key file does not exist: {public_key}")
        key_line = public_key.read_text(encoding="utf-8").strip()
        if not key_line.startswith("ssh-ed25519 "):
            fail("PUBLIC_KEY_FILE must contain an ed25519 public key")
    values["PUBLIC_KEY_FILE"] = str(public_key)
    values.setdefault("DEPLOY_COMMIT", "HEAD")
    return values


def save_resources(data):
    lab.atomic_write(RESOURCES, json.dumps(data, indent=2) + "\n")


def read_resources():
    if not RESOURCES.exists():
        fail(f"missing {RESOURCES}; no tracked W3 resources")
    try:
        data = json.loads(RESOURCES.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"invalid resources file: {exc}")
    if data.get("course") != lab.COURSE or data.get("week") != "w03":
        fail("resource file course/week does not match W3")
    return data


def tags(config):
    return {"course": lab.COURSE, "week": "w03", "group": config["GROUP_NAME"],
            "owner": config["OWNER_CODE"]}


def tag_args(resource_type, resource_tags):
    return ["--tag-specifications", json.dumps({
        "ResourceType": resource_type,
        "Tags": [{"Key": key, "Value": value} for key, value in resource_tags.items()],
    })]


def aws(args, region):
    return lab.run_aws(args, region)


def require_approved(config, action):
    resource_text = (
        f"W3 {action}\n"
        f"region: {config['AWS_REGION']}\n"
        f"VPC: {config['VPC_ID']} / subnet: {config['SUBNET_ID']} / AMI: {config['AMI_ID']}\n"
        f"source: {config['SOURCE_CIDR']}\n"
        f"group: {config['GROUP_NAME']} / owner: {config['OWNER_CODE']}\n"
        "resources: one security group, one imported key pair, one t3.micro instance\n"
        "network exposure: TCP 22 and 80 from the supplied /32 only\n"
        "storage: encrypted gp3 root volume with DeleteOnTermination; IMDSv2 required\n"
        "type APPROVE to continue, or anything else to cancel"
    )
    lab.approve(resource_text, "APPROVE")


def build_user_data(config):
    commit = config["DEPLOY_COMMIT"]
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", commit + "^{commit}"],
        cwd=ROOT, text=True).strip()
    output = LOCAL / f"w03-user-data-{sha}.sh"
    if not output.exists():
        subprocess.run(["bash", "deploy/make-user-data.sh", commit, str(output)],
                       cwd=ROOT, check=True)
    return sha, output


def describe_instance(instance_id, region):
    result = aws(["ec2", "describe-instances", "--instance-ids", instance_id], region)
    instances = result.get("Reservations", [{}])[0].get("Instances", [])
    if not instances:
        fail(f"instance not found: {instance_id}")
    return instances[0]


def record_instance_parts(data, instance):
    mappings = instance.get("BlockDeviceMappings", [])
    interfaces = instance.get("NetworkInterfaces", [])
    data["ebs_volume_id"] = mappings[0].get("Ebs", {}).get("VolumeId") if mappings else None
    data["eni_id"] = interfaces[0].get("NetworkInterfaceId") if interfaces else None
    data["public_ip"] = instance.get("PublicIpAddress")


def finish_up(data, config):
    instance_id = data.get("instance_id")
    if not instance_id:
        fail("creating resources file has no instance_id; inspect resources before continuing")
    aws(["ec2", "wait", "instance-running", "--instance-ids", instance_id], config["AWS_REGION"])
    instance = describe_instance(instance_id, config["AWS_REGION"])
    record_instance_parts(data, instance)
    data["status"] = "running"
    save_resources(data)
    check_health(data["public_ip"], data["commit"])
    print(json.dumps({"instance_id": instance_id, "public_ip": data["public_ip"],
                      "version": data["commit"]}, indent=2))


def up():
    config = load_config()
    if RESOURCES.exists():
        previous = read_resources()
        if previous.get("status") == "creating" and previous.get("instance_id"):
            ctx = lab.verify()
            if ctx["region"] != config["AWS_REGION"]:
                fail("config AWS_REGION does not match verified Learner Lab context")
            print("Resuming recorded W3 instance; no new resources will be created.")
            finish_up(previous, config)
            return
        if previous.get("status") != "deleted":
            fail("resources.json already exists; use down.sh or inspect it before proceeding")
    ctx = lab.verify()
    if ctx["region"] != config["AWS_REGION"]:
        fail("config AWS_REGION does not match verified Learner Lab context")
    sha, user_data = build_user_data(config)
    resource_tags = tags(config)
    require_approved(config, "create")
    data = {"course": lab.COURSE, "week": "w03", "region": config["AWS_REGION"],
            "group": config["GROUP_NAME"], "owner": config["OWNER_CODE"],
            "commit": sha, "status": "creating", "tags": resource_tags}
    save_resources(data)
    sg = aws(["ec2", "create-security-group", "--group-name",
              f"w03-{config['GROUP_NAME']}-{config['OWNER_CODE']}", "--description",
              "W3 inspection service security group", "--vpc-id", config["VPC_ID"],
              *tag_args("security-group", resource_tags)], config["AWS_REGION"])
    data["security_group_id"] = sg["GroupId"]
    save_resources(data)
    permissions = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                    "IpRanges": [{"CidrIp": config["SOURCE_CIDR"], "Description": "verified Codespace /32"}]}
                   for port in (22, 80)]
    aws(["ec2", "authorize-security-group-ingress", "--group-id", data["security_group_id"],
         "--ip-permissions", json.dumps(permissions)], config["AWS_REGION"])
    public_key = Path(config["PUBLIC_KEY_FILE"]).read_text(encoding="utf-8").strip()
    aws(["ec2", "import-key-pair", "--key-name", config["KEY_NAME"],
         "--public-key-material", "fileb://" + config["PUBLIC_KEY_FILE"],
         *tag_args("key-pair", resource_tags)], config["AWS_REGION"])
    data["key_name"] = config["KEY_NAME"]
    save_resources(data)
    block_device = [{"DeviceName": "/dev/xvda", "Ebs": {
        "VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True,
        "DeleteOnTermination": True}}]
    instance = aws(["ec2", "run-instances", "--image-id", config["AMI_ID"],
                    "--instance-type", "t3.micro", "--count", "1", "--subnet-id",
                    config["SUBNET_ID"], "--security-group-ids", data["security_group_id"],
                    "--key-name", config["KEY_NAME"], "--user-data", "fileb://" + str(user_data),
                    "--block-device-mappings", json.dumps(block_device),
                    "--metadata-options", "HttpTokens=required,HttpEndpoint=enabled",
                    *tag_args("instance", resource_tags)], config["AWS_REGION"])
    instance_id = instance["Instances"][0]["InstanceId"]
    data["instance_id"] = instance_id
    save_resources(data)
    finish_up(data, config)


def check_health(public_ip, expected_sha):
    if not public_ip:
        fail("instance has no public IPv4 address")
    url = f"http://{public_ip}/health"
    last_error = "no response"
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                body = json.loads(response.read())
                if response.status == 200 and body.get("status") == "ok" and body.get("version") == expected_sha:
                    return body
                last_error = f"HTTP {response.status}, version/status mismatch"
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(2)
    fail(f"health check failed for {url}: {last_error}")


def verify_tags(resource_id, resource_type, expected, region):
    if resource_type == "instance":
        item = describe_instance(resource_id, region)
    elif resource_type == "security-group":
        result = aws(["ec2", "describe-security-groups", "--group-ids", resource_id], region)
        items = result.get("SecurityGroups", [])
        if not items:
            fail(f"security group not found: {resource_id}")
        item = items[0]
    else:
        return
    actual = {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}
    if any(actual.get(key) != value for key, value in expected.items()):
        fail(f"tag ownership check failed for {resource_type} {resource_id}")


def resource_exists(args, region):
    try:
        result = aws(args, region)
    except lab.LabError as exc:
        if "NotFound" in str(exc):
            return False
        raise
    return bool(result.get("Volumes") or result.get("NetworkInterfaces"))


def stop():
    config = load_config(require_public_key=False)
    data = read_resources()
    ctx = lab.verify()
    instance_id = data.get("instance_id")
    if not instance_id:
        fail("resources file has no instance_id")
    verify_tags(instance_id, "instance", data["tags"], ctx["region"])
    require_approved(config, "stop instance only")
    aws(["ec2", "stop-instances", "--instance-ids", instance_id], ctx["region"])
    aws(["ec2", "wait", "instance-stopped", "--instance-ids", instance_id], ctx["region"])
    data["status"] = "stopped"
    save_resources(data)
    print(f"Verified stopped: {instance_id}")


def down():
    config = load_config(require_public_key=False)
    data = read_resources()
    ctx = lab.verify()
    expected = data["tags"]
    instance_id = data.get("instance_id")
    sg_id = data.get("security_group_id")
    key_name = data.get("key_name")
    require_approved(config, "delete recorded resources")
    verify_tags(instance_id, "instance", expected, ctx["region"])
    verify_tags(sg_id, "security-group", expected, ctx["region"])
    aws(["ec2", "terminate-instances", "--instance-ids", instance_id], ctx["region"])
    aws(["ec2", "wait", "instance-terminated", "--instance-ids", instance_id], ctx["region"])
    for volume_id in (data.get("ebs_volume_id"),):
        if volume_id and resource_exists(["ec2", "describe-volumes", "--volume-ids", volume_id], ctx["region"]):
            fail(f"EBS volume still exists after termination: {volume_id}")
    for eni_id in (data.get("eni_id"),):
        if eni_id and resource_exists(["ec2", "describe-network-interfaces", "--network-interface-ids", eni_id], ctx["region"]):
            fail(f"ENI still exists after termination: {eni_id}")
    aws(["ec2", "delete-security-group", "--group-id", sg_id], ctx["region"])
    aws(["ec2", "delete-key-pair", "--key-name", key_name], ctx["region"])
    data["status"] = "deleted"
    save_resources(data)
    print("Verified instance, EBS, ENI, security group, and key pair removal.")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"up", "stop", "down"}:
        fail("usage: w3_aws.py {up|stop|down}")
    try:
        {"up": up, "stop": stop, "down": down}[sys.argv[1]]()
    except (lab.LabError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()