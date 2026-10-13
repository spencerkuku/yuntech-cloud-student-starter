#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
LOCAL="$ROOT/practice/spencerku/w5/.local"
CONFIG="$LOCAL/w5.env"
RESOURCES="$LOCAL/resources.json"
mkdir -p "$LOCAL"

fail() { printf 'STOP: %s\n' "$1" >&2; exit 1; }
[[ -f "$CONFIG" ]] || fail "missing $CONFIG; copy w5.env.example and fill verified values"

# shellcheck disable=SC1090
source "$CONFIG"
: "${AWS_REGION:?w5.env must define AWS_REGION}"
: "${INSTANCE_ID:?w5.env must define INSTANCE_ID}"
: "${GROUP_NAME:?w5.env must define GROUP_NAME}"
: "${OWNER_CODE:?w5.env must define OWNER_CODE}"

python3 - "$ROOT" "$LOCAL" "$RESOURCES" "$AWS_REGION" "$INSTANCE_ID" "$GROUP_NAME" "$OWNER_CODE" <<'PY'
import ipaddress
import json
import secrets
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1])
local = Path(sys.argv[2])
resources_path = Path(sys.argv[3])
region, instance_id, group, owner = sys.argv[4:8]
sys.path.insert(0, str(root / "scripts"))
import lab


def fail(message):
    raise SystemExit("STOP: " + message)


def save(data):
    lab.atomic_write(Path(resources_path), json.dumps(data, indent=2) + "\n")


def tags(week):
    return {"course": lab.COURSE, "week": week, "group": group, "owner": owner}


def tag_args(resource_type, resource_tags):
    return ["--tag-specifications", json.dumps({
        "ResourceType": resource_type,
        "Tags": [{"Key": key, "Value": value} for key, value in resource_tags.items()],
    })]


ctx = lab.verify()
if ctx["region"] != region:
    fail("w5.env AWS_REGION does not match verified Learner Lab region")
if Path(resources_path).exists():
    data = json.loads(Path(resources_path).read_text(encoding="utf-8"))
    if data.get("week") == "w05" and data.get("status") not in ("deleted", "failed"):
        fail("W5 resources.json already records resources; inspect before rerunning")
else:
    data = {"course": lab.COURSE, "week": "w05", "region": region, "group": group,
            "owner": owner, "status": "creating", "tags": tags("w05")}

instance = lab.run_aws(["ec2", "describe-instances", "--instance-ids", instance_id], region)
instances = instance.get("Reservations", [{}])[0].get("Instances", [])
if len(instances) != 1:
    fail("recorded EC2 instance was not found")
host = instances[0]
vpc_id = host.get("VpcId")
host_subnet = host.get("SubnetId")
host_sg = host.get("SecurityGroups", [{}])[0].get("GroupId")
if not vpc_id or not host_subnet or not host_sg:
    fail("EC2 does not provide the required VPC, subnet, or security group")

vpc = lab.run_aws(["ec2", "describe-vpcs", "--vpc-ids", vpc_id], region)["Vpcs"][0]
vpc_network = ipaddress.ip_network(vpc["CidrBlock"])
existing = [
    ipaddress.ip_network(item["CidrBlock"])
    for item in lab.run_aws(["ec2", "describe-subnets", "--filters", "Name=vpc-id,Values=" + vpc_id],
                            region).get("Subnets", [])
]
azs = [
    item["ZoneName"]
    for item in lab.run_aws(["ec2", "describe-availability-zones",
                             "--filters", "Name=state,Values=available"], region).get("AvailabilityZones", [])
    if item.get("ZoneName")
]
selected = []
for subnet in vpc_network.subnets(new_prefix=24):
    if any(subnet.overlaps(item) for item in existing):
        continue
    selected.append(subnet)
    if len(selected) == 2:
        break
if len(selected) != 2 or len(set(azs[:2])) != 2:
    fail("could not find two non-overlapping /24 ranges and two AZs in the VPC")

resource_tags = tags("w05")
print(
    f"W5 RDS create: region={region}, VPC={vpc_id}, "
    "resources=2 private /24 subnets, local-only route table, DB subnet group, "
    "SG-db, encrypted non-public single-AZ PostgreSQL db.t3.micro (20 GiB gp3); "
    f"SG-db ingress=TCP 5432 from EC2 SG {host_sg}; "
    "recovery=stop EC2 and RDS, retain network resources"
)
if __import__("os").environ.get("W5_APPROVED") != "1":
    fail("review the printed resource scope and rerun with W5_APPROVED=1")

data.update({"vpc_id": vpc_id, "host_subnet_id": host_subnet, "host_security_group_id": host_sg,
             "subnet_cidrs": [str(item) for item in selected], "availability_zones": azs[:2]})
save(data)

for index, (cidr, az) in enumerate(zip(selected, azs[:2]), start=1):
    result = lab.run_aws(["ec2", "create-subnet", "--vpc-id", vpc_id,
                          "--cidr-block", str(cidr), "--availability-zone", az,
                          *tag_args("subnet", resource_tags)], region)
    data[f"private_subnet_{index}_id"] = result["Subnet"]["SubnetId"]
    save(data)

route_table = lab.run_aws(["ec2", "create-route-table", "--vpc-id", vpc_id,
                           *tag_args("route-table", resource_tags)], region)
data["private_route_table_id"] = route_table["RouteTable"]["RouteTableId"]
save(data)
for index in (1, 2):
    lab.run_aws(["ec2", "associate-route-table", "--route-table-id", data["private_route_table_id"],
                 "--subnet-id", data[f"private_subnet_{index}_id"]], region)

subnet_group_name = f"w05-{group}-{owner}-db"
lab.run_aws(["rds", "create-db-subnet-group", "--db-subnet-group-name", subnet_group_name,
             "--db-subnet-group-description", "W5 private inspection database",
             "--subnet-ids", data["private_subnet_1_id"], data["private_subnet_2_id"],
             "--tags", *sum((["Key=" + key + ",Value=" + value] for key, value in resource_tags.items()), [])],
            region)
data["db_subnet_group_name"] = subnet_group_name
save(data)

sg = lab.run_aws(["ec2", "create-security-group", "--group-name", f"w05-db-{group}-{owner}",
                  "--description", "W5 private PostgreSQL database", "--vpc-id", vpc_id,
                  *tag_args("security-group", resource_tags)], region)
data["db_security_group_id"] = sg["GroupId"]
save(data)
lab.run_aws(["ec2", "authorize-security-group-ingress", "--group-id", data["db_security_group_id"],
             "--ip-permissions", json.dumps([{
                 "IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                 "UserIdGroupPairs": [{"GroupId": host_sg, "Description": "EC2 host SG only"}],
             }])], region)

db_password = secrets.token_urlsafe(32)
db_env = Path(local) / "db.env"
lab.atomic_write(db_env, "DB_HOST=\nDB_NAME=inspection\nDB_USER=inspection\nDB_PASSWORD=" + db_password + "\n")
db_env.chmod(0o600)
identifier = f"w05-{group}-{owner}-inspection"
request = {
    "DBInstanceIdentifier": identifier,
    "DBInstanceClass": "db.t3.micro",
    "Engine": "postgres",
    "MasterUsername": "inspection",
    "MasterUserPassword": db_password,
    "AllocatedStorage": 20,
    "StorageType": "gp3",
    "DBName": "inspection",
    "DBSubnetGroupName": subnet_group_name,
    "VpcSecurityGroupIds": [data["db_security_group_id"]],
    "PubliclyAccessible": False,
    "StorageEncrypted": True,
    "MultiAZ": False,
    "BackupRetentionPeriod": 1,
    "Tags": [{"Key": key, "Value": value} for key, value in resource_tags.items()],
}
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=local, prefix=".rds-", delete=False) as stream:
    json.dump(request, stream)
    request_path = stream.name
try:
    result = lab.run_aws(["rds", "create-db-instance", "--cli-input-json", "file://" + request_path], region)
finally:
    Path(request_path).unlink(missing_ok=True)
data["db_instance_identifier"] = identifier
data["db_endpoint"] = None
save(data)
print("RDS creation requested; wait for available before deployment.")
while True:
    result = lab.run_aws(["rds", "describe-db-instances", "--db-instance-identifier", identifier], region)
    db = result["DBInstances"][0]
    status = db.get("DBInstanceStatus")
    if status == "available":
        data["db_endpoint"] = db["Endpoint"]["Address"]
        data["db_port"] = db["Endpoint"]["Port"]
        lab.atomic_write(db_env, "\n".join([
            "DB_HOST=" + data["db_endpoint"],
            "DB_NAME=inspection",
            "DB_USER=inspection",
            "DB_PASSWORD=" + db_password,
            "",
        ]))
        db_env.chmod(0o600)
        data["status"] = "available"
        save(data)
        break
    if status in ("failed", "incompatible-restore", "incompatible-network"):
        fail("RDS entered a terminal failure state: " + status)
    print("RDS status: " + str(status))
    import time
    time.sleep(30)
print(json.dumps({"db_instance_identifier": identifier[-4:],
                  "status": "available", "publicly_accessible": db["PubliclyAccessible"]}))
if db["PubliclyAccessible"] is not False:
    fail("RDS is publicly accessible")
PY
