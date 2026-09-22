#!/usr/bin/env python3
"""deploy/up.py — W3 deploy round: create exactly 1 SG, 1 imported ed25519 key pair, 1 instance.

Reads .local/config (SOURCE_IP, GROUP, OWNER; optional SUBNET_ID, DEPLOY_COMMIT).
Writes every created ID into .local/resources.json immediately (kept out of Git).
Every AWS call goes through scripts/lab.py run_aws so requests cannot be redirected
by ambient environment variables. No secrets or private keys are read or printed.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lab import run_aws, context, LabError  # noqa: E402

COURSE = "yuntech-115-1"
WEEK = "w03"
CONFIG_PATH = ROOT / ".local" / "config"
RESOURCES_PATH = ROOT / ".local" / "resources.json"
SSH_DIR = Path.home() / ".ssh"


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config():
    if not CONFIG_PATH.is_file():
        sys.exit(f"STOP: {CONFIG_PATH} does not exist. Create it with SOURCE_IP/GROUP/OWNER "
                 "(see labs/03-service-prototype/README.md).")
    cfg = {}
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cfg[key.strip()] = value.strip()
    for key in ("SOURCE_IP", "GROUP", "OWNER"):
        if not cfg.get(key):
            sys.exit(f"STOP: {key} missing in {CONFIG_PATH}")
    if not re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", cfg["SOURCE_IP"]):
        sys.exit("STOP: SOURCE_IP must be a bare IPv4 address (no CIDR)")
    return cfg


def load_resources():
    if not RESOURCES_PATH.is_file():
        return {"rounds": []}
    return json.loads(RESOURCES_PATH.read_text(encoding="utf-8"))


def save_resources(data):
    RESOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="resources-", dir=RESOURCES_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, RESOURCES_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def confirm(prompt, token):
    print(prompt)
    if not sys.stdin.isatty():
        sys.exit("STOP: no interactive terminal. Run this script yourself, do not wire it to an agent.")
    answer = input(f"Type exactly {token!r} to proceed: ").strip()
    if answer != token:
        sys.exit("Cancelled; nothing was created.")


def verify_identity_gate():
    result = subprocess.run(["bash", "scripts/verify-aws.sh"], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("STOP: identity gate failed. See scripts/verify-aws.sh output in your terminal.")
    print(result.stdout.strip())


def describe_instance(region, iid):
    try:
        rows = run_aws(["ec2", "describe-instances", "--instance-ids", iid], region)["Reservations"]
        return rows[0]["Instances"][0] if rows else None
    except LabError as exc:
        if "NotFound" in str(exc):
            return None
        raise


def wait_until(region, description, predicate, timeout, interval=5, expected="ok"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate(region)
        except LabError as exc:
            last = None
            if "NotFound" not in str(exc):
                raise
        if last:
            print(f"OBS ok {description}: {utc_now()}")
            return last
        time.sleep(interval)
    sys.exit(f"STOP: timeout after {timeout}s waiting for {description} (last={last!r})")


def run_curl(url, timeout=8):
    """Return (curl_exit_code, http_code, body). http_code '000' means no HTTP response."""
    result = subprocess.run(["curl", "-sS", "--max-time", str(timeout),
                             "-w", "|%{http_code}", url], capture_output=True, text=True)
    text = result.stdout
    if "|" in text:
        body, _, code = text.rpartition("|")
    else:
        body, code = text, "000"
    return result.returncode, code.strip(), body.strip()


def health_check(region, url, commit):
    """Poll until /health is 200 and version == commit. Returns the parsed body."""
    shadow = {"last": None}

    def probe(region):
        exit_code, http, body = run_curl(url)
        shadow["last"] = (exit_code, http)
        if exit_code == 0 and http == "200":
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return None
            if data.get("version") == commit:
                return data
        return None

    result = wait_until(region, f"/health 200 version=={commit}", probe, timeout=180, interval=5)
    return result


def main():
    cfg = load_config()
    ctx = context()
    region = ctx["region"]
    src = cfg["SOURCE_IP"]
    group = cfg["GROUP"]
    owner = cfg["OWNER"]
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", owner)
    commit = cfg.get("DEPLOY_COMMIT") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        sys.exit("STOP: DEPLOY_COMMIT must be a 40-character commit SHA.")

    resources = load_resources()
    for index, rnd in enumerate(resources["rounds"], 1):
        state = rnd.get("instance", {}).get("state")
        if state and state != "terminated":
            sys.exit(f"STOP: round {index} instance {rnd['instance']['id']} is {state}; "
                     "run deploy/down.sh first (only one host at a time).")
    if not commit:
        sys.exit("STOP: cannot resolve a deploy commit.")

    # Resolve AMI (latest AL2023 x86_64) at runtime; do not hardcode an account image.
    amis = run_aws(["ec2", "describe-images", "--owners", "amazon",
                    "--filters", "Name=name,Values=al2023-ami-2023.*-x86_64",
                    "Name=state,Values=available"], region)["Images"]
    if not amis:
        sys.exit("STOP: no AL2023 x86_64 AMI found; ask for help before proceeding.")
    ami = max(amis, key=lambda item: item["CreationDate"])
    ami_id = ami["ImageId"]

    # Resolve the subnet inside the default VPC.
    if cfg.get("SUBNET_ID"):
        subnets = run_aws(["ec2", "describe-subnets", "--subnet-ids", cfg["SUBNET_ID"]], region)["Subnets"]
        if not subnets:
            sys.exit("STOP: SUBNET_ID in config does not exist.")
        subnet = subnets[0]
        subnet_id = subnet["SubnetId"]
    else:
        vpcs = run_aws(["ec2", "describe-vpcs", "--filters", "Name=is-default,Values=true"], region)["Vpcs"]
        if not vpcs:
            sys.exit("STOP: no default VPC; do not create one. Ask for help.")
        subs = run_aws(["ec2", "describe-subnets", "--filters",
                        "Name=vpc-id,Values=" + vpcs[0]["VpcId"],
                        "Name=default-for-az,Values=true"], region)["Subnets"]
        if not subs:
            sys.exit("STOP: no default subnet; ask for help.")
        subnet = sorted(subs, key=lambda s: s["AvailabilityZone"])[0]
        subnet_id = subnet["SubnetId"]
    vpc_id = subnet["VpcId"]
    az = subnet["AvailabilityZone"]

    # Verify this subnet is public: effective route table has 0.0.0.0/0 -> igw-*.
    rts = run_aws(["ec2", "describe-route-tables", "--filters", "Name=vpc-id,Values=" + vpc_id],
                  region)["RouteTables"]
    explicit, main_rt = None, None
    for rt in rts:
        for assoc in rt.get("Associations", []):
            if assoc.get("SubnetId") == subnet_id:
                explicit = rt
            if assoc.get("Main"):
                main_rt = rt
    effective = explicit or main_rt
    to_internet = [r for r in (effective or {}).get("Routes", [])
                   if r.get("DestinationCidrBlock") == "0.0.0.0/0"
                   and (r.get("GatewayId") or "").startswith("igw-")]
    if not to_internet:
        sys.exit("STOP: subnet has no 0.0.0.0/0 -> igw-* route; it is not public. Do not create a VPC/IGW.")

    key_name = f"w03-{slug}-key"
    sg_name = f"w03-{slug}-sg"
    inst_name = f"w03-{slug}-instance"
    key_path = SSH_DIR / key_name

    print("\n===== Resources to CREATE (this round) =====")
    print(f" 1. Key pair (imported): {key_name}  [ed25519 public key from {key_path}.pub]")
    print(f" 2. Security group:      {sg_name}")
    print(f"    inbound TCP 22, TCP 80 ONLY from {src}/32 (no 0.0.0.0/0, no ::/0)")
    print(f" 3. Instance:            {inst_name}")
    print(f"    AMI {ami_id} ({ami['Name']})  t3.micro  subnet {subnet_id} (AZ {az}, VPC {vpc_id})")
    print("    root /dev/xvda: 8 GiB gp3 Encrypted DeleteOnTermination; IMDSv2 required")
    print("    user data from deploy/make-user-data.sh (packages committed app files only)")
    print(f"    tags: course={COURSE} week={WEEK} group={group} owner={owner}")
    print(f" 4. Deploy commit:       {commit}")
    print("Cost: t3.micro on-demand + 8 GiB gp3 + in-use public IPv4 (see official price pages).")
    confirm("\nProceed?", "CREATE")

    verify_identity_gate()
    print(f"\n{utc_now()} Starting deployment of commit {commit} ...")

    # --- 1. SSH key pair (private key stays in ~/.ssh, never shown/committed) ---
    SSH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not key_path.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"w03-{owner}",
                        "-f", str(key_path)], check=True)
        os.chmod(key_path, 0o600)
    pub_text = (key_path.with_suffix(".pub")).read_text(encoding="utf-8").strip()
    kp_resp = run_aws(["ec2", "import-key-pair", "--key-name", key_name,
                       "--public-key-material", "fileb://" + str(key_path.with_suffix(".pub"))], region)
    kp_id = kp_resp["KeyPairId"]
    run_aws(["ec2", "create-tags", "--resources", kp_id, "--tags", json.dumps(
        [{"Key": "course", "Value": COURSE}, {"Key": "week", "Value": WEEK},
         {"Key": "group", "Value": group}, {"Key": "owner", "Value": owner}])], region)
    print(f"OBS created key pair {key_name} ({kp_id}): {utc_now()}")
    round_data = {"round": len(resources["rounds"]) + 1, "created_utc": utc_now(), "commit": commit,
                  "subnet_id": subnet_id, "az": az,
                  "key_pair": {"id": kp_id, "name": key_name}}
    resources["rounds"].append(round_data)
    save_resources(resources)

    # --- 2. Security group: inbound 22/80 from src/32 only ---
    sg_resp = run_aws(["ec2", "create-security-group", "--group-name", sg_name,
                       "--description", "W3 inspection service; 22/80 from Codespace /32",
                       "--vpc-id", vpc_id], region)
    sg_id = sg_resp["GroupId"]
    round_data["security_group"] = {"id": sg_id, "name": sg_name}
    save_resources(resources)  # record now so a later failure is reclaimable via down.sh
    run_aws(["ec2", "authorize-security-group-ingress", "--group-id", sg_id,
             "--ip-permissions", json.dumps([
                 {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                  "IpRanges": [{"CidrIp": f"{src}/32"}]},
                 {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                  "IpRanges": [{"CidrIp": f"{src}/32"}]}])], region)
    run_aws(["ec2", "create-tags", "--resources", sg_id, "--tags", json.dumps(
        [{"Key": "course", "Value": COURSE}, {"Key": "week", "Value": WEEK},
         {"Key": "group", "Value": group}, {"Key": "owner", "Value": owner}])], region)
    print(f"OBS created security group {sg_name} ({sg_id}): {utc_now()}")

    # --- 3. Build user data from the deploy commit (committed files only) ---
    # make_user_data.py opens its output with "xb" (exclusive create);
    # drop any stale artifact so T4 rebuilds do not fail on the old file.
    user_data_path = ROOT / ".local" / "w03-user-data.sh"
    user_data_path.unlink(missing_ok=True)
    built = subprocess.run(["bash", "deploy/make-user-data.sh", commit, str(user_data_path)],
                           cwd=ROOT, capture_output=True, text=True)
    if built.returncode != 0:
        sys.exit("STOP: make-user-data.sh failed: " + built.stderr.strip())
    print(built.stdout.strip())

    # --- 4. Instance ---
    inst = run_aws(["ec2", "run-instances",
                    "--image-id", ami_id,
                    "--instance-type", "t3.micro",
                    "--subnet-id", subnet_id,
                    "--security-group-ids", sg_id,
                    "--key-name", key_name,
                    "--block-device-mappings", json.dumps([{
                        "DeviceName": "/dev/xvda",
                        "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True,
                                "DeleteOnTermination": True}}]),
                    "--metadata-options", json.dumps({"HttpTokens": "required",
                                                      "HttpEndpoint": "enabled"}),
                    "--user-data", "fileb://" + str(user_data_path),
                    "--tag-specifications", json.dumps([{
                        "ResourceType": "instance", "Tags": [
                            {"Key": "Name", "Value": inst_name},
                            {"Key": "course", "Value": COURSE},
                            {"Key": "week", "Value": WEEK},
                            {"Key": "group", "Value": group},
                            {"Key": "owner", "Value": owner}]}])], region)
    iid = inst["Instances"][0]["InstanceId"]
    print(f"OBS created instance {iid}: {utc_now()}")
    round_data["instance"] = {"id": iid, "state": "pending"}
    save_resources(resources)

    # --- 5. Observe layer 1: running ---
    def running(region):
        detail = describe_instance(region, iid)
        if detail and detail["State"]["Name"] == "running":
            return detail
        return None

    detail = wait_until(region, f"instance {iid} running", running, timeout=180, interval=5)
    round_data["instance"]["state"] = "running"
    round_data["public_ip"] = detail.get("PublicIpAddress")
    round_data["eni"] = {"id": detail["NetworkInterfaces"][0]["NetworkInterfaceId"]}
    round_data["root_volume"] = {"id": next(
        b["Ebs"]["VolumeId"] for b in detail["BlockDeviceMappings"]
        if b["DeviceName"] == "/dev/xvda")}
    save_resources(resources)

    public_ip = round_data["public_ip"]
    if not public_ip:
        sys.exit("STOP: instance running without a public IPv4; check subnet MapPublicIpOnLaunch.")
    url = f"http://{public_ip}/health"
    print(f"Instance public IPv4: {public_ip}")

    # Early curl: exactly one attempt right after "running" (per task card T2 step 3).
    exit_code, http, body = run_curl(url)
    print(f"[early curl @ {utc_now()}] exit={exit_code} http={http} "
          f"body={body[:120]!r}  (HTTP 000 = no HTTP response; nonzero exit = transport)")

    # --- 6. Observe layer 2: status checks 2/2 ---
    def checks_ok(region):
        rows = run_aws(["ec2", "describe-instance-status", "--instance-ids", iid],
                       region).get("InstanceStatuses", [])
        if not rows:
            return None
        return rows[0] if (rows[0]["InstanceStatus"]["Status"] == "ok"
                           and rows[0]["SystemStatus"]["Status"] == "ok") else None

    wait_until(region, f"instance {iid} status checks 2/2", checks_ok, timeout=300, interval=10)

    # --- 7. Observe layer 5: /health 200 and version == commit ---
    health = health_check(region, url, commit)
    round_data["health_ok_utc"] = utc_now()
    save_resources(resources)

    print("\n===== OBSERVATIONS (first observed UTC) =====")
    print(f" running:            recorded when polled above")
    print(f" status checks 2/2:  recorded above")
    print(f" /health 200 v=={commit[:10]}...: {round_data['health_ok_utc']}")
    print(f" installed commit:   {health['version']}  started_at: {health['started_at']}")
    print("\nNext (T2 layers 3-4, via SSH): connect with the private key, verify the host"
          " fingerprint, then run `cloud-init status --wait` and the port checks on the host.")
    print("Resources are recorded in .local/resources.json; reclaim with deploy/down.sh.")


if __name__ == "__main__":
    try:
        main()
    except LabError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        print("Note: .local/resources.json holds IDs created so far; use deploy/down.sh to reclaim them.",
              file=sys.stderr)
        sys.exit(1)