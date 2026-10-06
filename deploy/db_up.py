#!/usr/bin/env python3
"""Plan or create this learner's private W5 PostgreSQL resources."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import lab

LOCAL = ROOT / ".local"
RESOURCE_FILE = LOCAL / "resources.json"
DB_ENV_FILE = LOCAL / "db.env"


def save_resources(resources):
    if LOCAL.is_symlink():
        raise lab.LabError("Refusing a symlink for the .local directory.")
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RESOURCE_FILE.is_symlink():
        raise lab.LabError("Refusing a symlink at .local/resources.json.")
    fd, temp_name = tempfile.mkstemp(prefix=".resources-", dir=LOCAL)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(resources, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temp_name, RESOURCE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_resources():
    if not RESOURCE_FILE.exists():
        return {}
    if RESOURCE_FILE.is_symlink():
        raise lab.LabError("Refusing a symlink at .local/resources.json.")
    try:
        data = json.loads(RESOURCE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise lab.LabError("Cannot safely read .local/resources.json; inspect it before continuing.") from exc
    if not isinstance(data, dict):
        raise lab.LabError(".local/resources.json must contain a JSON object.")
    return data


def save_db_env(values):
    if LOCAL.is_symlink():
        raise lab.LabError("Refusing a symlink for the .local directory.")
    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    if DB_ENV_FILE.is_symlink():
        raise lab.LabError("Refusing a symlink at .local/db.env.")
    content = "".join(f"{key}={value}\n" for key, value in values.items())
    fd, temp_name = tempfile.mkstemp(prefix=".db-env-", dir=LOCAL)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temp_name, DB_ENV_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_db_env():
    if not DB_ENV_FILE.exists():
        return None
    if DB_ENV_FILE.is_symlink() or DB_ENV_FILE.stat().st_mode & 0o777 != 0o600:
        raise lab.LabError(".local/db.env must be a regular file with mode 600.")
    values = {}
    for line in DB_ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def aws(region, args):
    return lab.run_aws(args, region)


def candidate_subnets(vpc_cidrs, existing_cidrs):
    existing = [ipaddress.ip_network(value) for value in existing_cidrs]
    choices = []
    for cidr in vpc_cidrs:
        network = ipaddress.ip_network(cidr)
        if network.version != 4 or network.prefixlen > 24:
            continue
        for candidate in network.subnets(new_prefix=24):
            if all(not candidate.overlaps(used) for used in existing):
                choices.append(str(candidate))
                existing.append(candidate)
                if len(choices) == 2:
                    return choices
    return choices


def instance_context(region, instance_id):
    result = aws(region, [
        "ec2", "describe-instances", "--instance-ids", instance_id,
    ])
    instances = [
        instance
        for reservation in result.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if len(instances) != 1:
        raise lab.LabError("The specified EC2 instance was not found uniquely.")
    instance = instances[0]
    if instance.get("State", {}).get("Name") not in {"running", "stopped"}:
        raise lab.LabError("EC2 must be running or stopped before W5 planning.")
    groups = instance.get("SecurityGroups", [])
    if len(groups) != 1:
        raise lab.LabError("Expected exactly one EC2 security group; review the instance before continuing.")
    vpc_id = instance.get("VpcId")
    subnet_id = instance.get("SubnetId")
    if not vpc_id or not subnet_id or not groups[0].get("GroupId"):
        raise lab.LabError("EC2 is missing VPC, subnet, or security-group information.")

    vpcs = aws(region, ["ec2", "describe-vpcs", "--vpc-ids", vpc_id]).get("Vpcs", [])
    if len(vpcs) != 1:
        raise lab.LabError("Could not uniquely read the EC2 VPC.")
    vpc = vpcs[0]
    vpc_cidrs = [
        item["CidrBlock"]
        for item in vpc.get("CidrBlockAssociationSet", [])
        if item.get("CidrBlockState", {}).get("State") == "associated"
    ] or [vpc["CidrBlock"]]
    subnets = aws(region, [
        "ec2", "describe-subnets", "--filters", f"Name=vpc-id,Values={vpc_id}",
    ]).get("Subnets", [])
    existing_cidrs = [subnet["CidrBlock"] for subnet in subnets]
    cidrs = candidate_subnets(vpc_cidrs, existing_cidrs)
    if len(cidrs) != 2:
        raise lab.LabError("Could not find two unused /24 IPv4 blocks in the EC2 VPC.")
    zones = aws(region, ["ec2", "describe-availability-zones"]).get("AvailabilityZones", [])
    az_names = sorted(
        zone["ZoneName"]
        for zone in zones
        if zone.get("State") == "available" and zone.get("ZoneType", "availability-zone") == "availability-zone"
    )
    if len(az_names) < 2:
        raise lab.LabError("Fewer than two available standard Availability Zones were returned.")
    return {
        "instance": instance,
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "instance_sg_id": groups[0]["GroupId"],
        "vpc_cidrs": vpc_cidrs,
        "subnets": subnets,
        "cidrs": cidrs,
        "azs": az_names[:2],
    }


def resource_tag_specs(resource_type, tags):
    return [{
        "ResourceType": resource_type,
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }]


def write_w5(resources, w5):
    resources["w5"] = w5
    save_resources(resources)


def tagged_name(instance_id, suffix):
    return f"inspection-w5-{instance_id[-8:].lower()}-{suffix}"


def create_subnet(region, resources, w5, index):
    entry = w5["subnets"][index]
    if entry.get("id"):
        return
    name = tagged_name(w5["instance_id"], f"private-{index + 1}")
    response = aws(region, [
        "ec2", "create-subnet",
        "--vpc-id", w5["vpc_id"],
        "--cidr-block", entry["cidr"],
        "--availability-zone", entry["az"],
        "--tag-specifications", json.dumps(resource_tag_specs("subnet", {
            "Name": name, "Course": lab.COURSE, "Week": "W5",
            "OwnerInstanceId": w5["instance_id"],
        })),
    ])
    entry["id"] = response["Subnet"]["SubnetId"]
    write_w5(resources, w5)


def create_route_table(region, resources, w5):
    if w5.get("route_table_id"):
        return
    name = tagged_name(w5["instance_id"], "private-rt")
    response = aws(region, [
        "ec2", "create-route-table", "--vpc-id", w5["vpc_id"],
        "--tag-specifications", json.dumps(resource_tag_specs("route-table", {
            "Name": name, "Course": lab.COURSE, "Week": "W5",
            "OwnerInstanceId": w5["instance_id"],
        })),
    ])
    w5["route_table_id"] = response["RouteTable"]["RouteTableId"]
    write_w5(resources, w5)


def associate_subnets(region, resources, w5):
    route_tables = aws(region, [
        "ec2", "describe-route-tables", "--route-table-ids", w5["route_table_id"],
    ]).get("RouteTables", [])
    associations = route_tables[0].get("Associations", []) if route_tables else []
    for entry in w5["subnets"]:
        existing = [item for item in associations if item.get("SubnetId") == entry["id"]]
        if entry.get("association_id"):
            if not existing or existing[0].get("RouteTableAssociationId") != entry["association_id"]:
                raise lab.LabError("A recorded private-subnet route-table association is missing or changed.")
            continue
        if existing:
            entry["association_id"] = existing[0]["RouteTableAssociationId"]
            write_w5(resources, w5)
            continue
        response = aws(region, [
            "ec2", "associate-route-table",
            "--route-table-id", w5["route_table_id"],
            "--subnet-id", entry["id"],
        ])
        entry["association_id"] = response["AssociationId"]
        write_w5(resources, w5)


def create_database_security_group(region, resources, w5):
    if w5.get("db_security_group_id"):
        return
    name = tagged_name(w5["instance_id"], "db-sg")
    response = aws(region, [
        "ec2", "create-security-group",
        "--group-name", name,
        "--description", "W5 PostgreSQL access from this inspection EC2 only",
        "--vpc-id", w5["vpc_id"],
        "--tag-specifications", json.dumps(resource_tag_specs("security-group", {
            "Name": name, "Course": lab.COURSE, "Week": "W5",
            "OwnerInstanceId": w5["instance_id"],
        })),
    ])
    w5["db_security_group_id"] = response["GroupId"]
    write_w5(resources, w5)


def allow_ec2_database_access(region, resources, w5):
    current = aws(region, [
        "ec2", "describe-security-group-rules",
        "--filters", f"Name=group-id,Values={w5['db_security_group_id']}",
    ]).get("SecurityGroupRules", [])
    matching = [
        rule for rule in current
        if rule.get("IsEgress") is False
        and rule.get("IpProtocol") == "tcp"
        and rule.get("FromPort") == 5432
        and rule.get("ToPort") == 5432
        and rule.get("ReferencedGroupInfo", {}).get("GroupId") == w5["instance_sg_id"]
    ]
    other_ingress = [rule for rule in current if rule.get("IsEgress") is False and rule not in matching]
    if other_ingress:
        raise lab.LabError("SG-db has unexpected ingress rules; inspect it before continuing.")
    if matching:
        w5["db_ingress_rule_id"] = matching[0].get("SecurityGroupRuleId", "authorized")
        write_w5(resources, w5)
        return
    response = aws(region, [
        "ec2", "authorize-security-group-ingress",
        "--group-id", w5["db_security_group_id"],
        "--ip-permissions", json.dumps([{
            "IpProtocol": "tcp",
            "FromPort": 5432,
            "ToPort": 5432,
            "UserIdGroupPairs": [{"GroupId": w5["instance_sg_id"]}],
        }]),
    ])
    rule_ids = response.get("SecurityGroupRules", [])
    w5["db_ingress_rule_id"] = rule_ids[0].get("SecurityGroupRuleId") if rule_ids else "authorized"
    write_w5(resources, w5)


def create_db_subnet_group(region, resources, w5):
    if w5.get("db_subnet_group_name"):
        return
    name = w5.get("planned_db_subnet_group_name") or tagged_name(w5["instance_id"], "db-subnets")
    aws(region, [
        "rds", "create-db-subnet-group",
        "--db-subnet-group-name", name,
        "--db-subnet-group-description", "W5 private PostgreSQL subnets across two AZs",
        "--subnet-ids", *(entry["id"] for entry in w5["subnets"]),
        "--tags", json.dumps([
            {"Key": "Course", "Value": lab.COURSE},
            {"Key": "Week", "Value": "W5"},
            {"Key": "OwnerInstanceId", "Value": w5["instance_id"]},
        ]),
    ])
    w5["db_subnet_group_name"] = name
    write_w5(resources, w5)


def create_rds(region, resources, w5):
    if w5.get("db_instance_identifier"):
        db_env = load_db_env()
        if db_env is None or not db_env.get("DB_PASSWORD"):
            raise lab.LabError("Recorded RDS exists but .local/db.env is missing its password; do not recreate it.")
        return
    db_env = load_db_env()
    if db_env is None:
        db_env = {
            "DB_HOST": "pending",
            "DB_NAME": "inspection",
            "DB_USER": "inspection_app",
            "DB_PASSWORD": secrets.token_urlsafe(36),
        }
        save_db_env(db_env)
    password = db_env.get("DB_PASSWORD", "")
    if not password:
        raise lab.LabError(".local/db.env has no DB_PASSWORD; refusing to create RDS.")

    identifier = w5.get("planned_db_instance_identifier") or tagged_name(w5["instance_id"], "db")
    parameters = {
        "DBInstanceIdentifier": identifier,
        "AllocatedStorage": 20,
        "StorageType": "gp3",
        "Engine": "postgres",
        "DBInstanceClass": "db.t3.micro",
        "MasterUsername": db_env["DB_USER"],
        "MasterUserPassword": password,
        "DBName": "inspection",
        "VpcSecurityGroupIds": [w5["db_security_group_id"]],
        "DBSubnetGroupName": w5["db_subnet_group_name"],
        "PubliclyAccessible": False,
        "StorageEncrypted": True,
        "MultiAZ": False,
        "BackupRetentionPeriod": 0,
        "AutoMinorVersionUpgrade": True,
        "Tags": [
            {"Key": "Course", "Value": lab.COURSE},
            {"Key": "Week", "Value": "W5"},
            {"Key": "OwnerInstanceId", "Value": w5["instance_id"]},
        ],
    }
    fd, temp_name = tempfile.mkstemp(prefix=".rds-create-", suffix=".json", dir=LOCAL)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(parameters, stream)
        aws(region, ["rds", "create-db-instance", "--cli-input-json", f"file://{temp_name}"])
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    w5["db_instance_identifier"] = identifier
    write_w5(resources, w5)


def wait_for_database(region, resources, w5):
    identifier = w5["db_instance_identifier"]
    deadline = time.monotonic() + 1200
    while True:
        instances = aws(region, [
            "rds", "describe-db-instances", "--db-instance-identifier", identifier,
        ]).get("DBInstances", [])
        if len(instances) != 1:
            raise lab.LabError("RDS instance could not be read back uniquely.")
        instance = instances[0]
        status = instance.get("DBInstanceStatus")
        if status == "available":
            if instance.get("PubliclyAccessible") is not False:
                raise lab.LabError("RDS readback is not private; stopping without further changes.")
            if (
                instance.get("Engine") != "postgres"
                or instance.get("DBInstanceClass") != "db.t3.micro"
                or instance.get("AllocatedStorage") != 20
                or instance.get("StorageType") != "gp3"
                or instance.get("StorageEncrypted") is not True
                or instance.get("MultiAZ") is not False
                or instance.get("DBName") != "inspection"
                or instance.get("DBSubnetGroup", {}).get("DBSubnetGroupName") != w5["db_subnet_group_name"]
                or {group.get("VpcSecurityGroupId") for group in instance.get("VpcSecurityGroups", [])}
                   != {w5["db_security_group_id"]}
            ):
                raise lab.LabError("RDS readback did not match the approved W5 specification.")
            subnet_groups = aws(region, [
                "rds", "describe-db-subnet-groups",
                "--db-subnet-group-name", w5["db_subnet_group_name"],
            ]).get("DBSubnetGroups", [])
            if len(subnet_groups) != 1:
                raise lab.LabError("DB subnet group did not read back uniquely.")
            actual_subnets = {
                subnet.get("SubnetIdentifier"): subnet.get("SubnetAvailabilityZone", {}).get("Name")
                for subnet in subnet_groups[0].get("Subnets", [])
            }
            expected_subnets = {entry["id"]: entry["az"] for entry in w5["subnets"]}
            if actual_subnets != expected_subnets or len(set(actual_subnets.values())) != 2:
                raise lab.LabError("DB subnet group did not read back as the approved two-AZ subnet set.")
            db_env = load_db_env() or {}
            endpoint = instance.get("Endpoint", {}).get("Address")
            if not endpoint:
                raise lab.LabError("RDS is available but has no endpoint in its readback.")
            db_env.update({
                "DB_HOST": endpoint,
                "DB_NAME": "inspection",
                "DB_USER": "inspection_app",
            })
            save_db_env(db_env)
            print("RDS readback: available; PubliclyAccessible=false; storage encrypted; Single-AZ.")
            print(f"RDS identifier suffix: {identifier[-4:]}")
            return
        if status in {"failed", "incompatible-parameters", "incompatible-network"}:
            raise lab.LabError(f"RDS stopped in terminal state {status}; inspect the recorded resource before retrying.")
        if time.monotonic() >= deadline:
            raise lab.LabError("RDS is still creating after 20 minutes; do not rerun; inspect its recorded identifier.")
        time.sleep(20)


def plan(region, instance_id):
    ctx = instance_context(region, instance_id)
    suffix = instance_id[-8:].lower()
    return {
        "instance_id": instance_id,
        "instance_state": ctx["instance"].get("State", {}).get("Name"),
        "vpc_id": ctx["vpc_id"],
        "subnet_id": ctx["subnet_id"],
        "instance_sg_id": ctx["instance_sg_id"],
        "cidrs": ctx["cidrs"],
        "azs": ctx["azs"],
        "subnets": [
            {"cidr": ctx["cidrs"][index], "az": ctx["azs"][index]}
            for index in range(2)
        ],
        "route_table_name": tagged_name(instance_id, "private-rt"),
        "db_security_group_name": tagged_name(instance_id, "db-sg"),
        "db_subnet_group_name": tagged_name(instance_id, "db-subnets"),
        "db_instance_identifier": f"inspection-w5-{suffix}-db",
    }, ctx


def expected_resource_check(region, proposed, w5):
    expected = w5 or {}
    subnet_specs = proposed["subnets"]
    for index, spec in enumerate(subnet_specs):
        name = tagged_name(proposed["instance_id"], f"private-{index + 1}")
        found = aws(region, [
            "ec2", "describe-subnets",
            "--filters", f"Name=vpc-id,Values={proposed['vpc_id']}",
            f"Name=tag:Name,Values={name}",
        ]).get("Subnets", [])
        recorded_id = expected.get("subnets", [{}] * 2)[index].get("id")
        if (recorded_id and not found) or any(subnet.get("SubnetId") != recorded_id for subnet in found):
            raise lab.LabError(f"An unrecorded W5 subnet named {name} exists; refusing duplicate creation.")

    checks = [
        ("route_table_id", "route-table", proposed["route_table_name"], ["ec2", "describe-route-tables"],
         ["--filters", f"Name=vpc-id,Values={proposed['vpc_id']}", f"Name=tag:Name,Values={proposed['route_table_name']}"], "RouteTables", "RouteTableId"),
        ("db_security_group_id", "security group", proposed["db_security_group_name"], ["ec2", "describe-security-groups"],
         ["--filters", f"Name=vpc-id,Values={proposed['vpc_id']}", f"Name=group-name,Values={proposed['db_security_group_name']}"], "SecurityGroups", "GroupId"),
    ]
    for ledger_key, kind, name, prefix, args, result_key, id_key in checks:
        found = aws(region, [*prefix, *args]).get(result_key, [])
        recorded_id = expected.get(ledger_key)
        if (recorded_id and not found) or any(item.get(id_key) != recorded_id for item in found):
            raise lab.LabError(f"An unrecorded W5 {kind} named {name} exists; refusing duplicate creation.")

    subnet_name = proposed["db_subnet_group_name"]
    try:
        found_groups = aws(region, [
            "rds", "describe-db-subnet-groups", "--db-subnet-group-name", subnet_name,
        ]).get("DBSubnetGroups", [])
    except lab.LabError as exc:
        if "DBSubnetGroupNotFoundFault" not in str(exc):
            raise
        found_groups = []
    recorded_subnet_group = expected.get("db_subnet_group_name")
    if (recorded_subnet_group and not found_groups) or (
        found_groups and recorded_subnet_group != subnet_name
    ):
        raise lab.LabError(f"An unrecorded W5 DB subnet group named {subnet_name} exists; refusing duplicate creation.")
    if found_groups:
        expected_subnets = {
            subnet.get("id")
            for subnet in expected.get("subnets", [])
            if subnet.get("id")
        }
        actual_subnets = {
            subnet.get("SubnetIdentifier")
            for subnet in found_groups[0].get("Subnets", [])
        }
        if expected_subnets and actual_subnets != expected_subnets:
            raise lab.LabError("The recorded DB subnet group contains an unexpected subnet set.")

    identifier = proposed["db_instance_identifier"]
    try:
        found_instances = aws(region, [
            "rds", "describe-db-instances", "--db-instance-identifier", identifier,
        ]).get("DBInstances", [])
    except lab.LabError as exc:
        if "DBInstanceNotFound" not in str(exc):
            raise
        found_instances = []
    recorded_instance = expected.get("db_instance_identifier")
    if (recorded_instance and not found_instances) or (
        found_instances and recorded_instance != identifier
    ):
        raise lab.LabError(f"An unrecorded W5 RDS instance {identifier} exists; refusing duplicate creation.")


def run(args):
    if not re.fullmatch(r"i-[0-9a-fA-F]{8,17}", args.instance_id):
        raise lab.LabError("instance-id must be an EC2 instance ID.")
    context = lab.verify()
    region = context["region"]
    resources = load_resources()
    existing = resources.get("w5")
    proposed, _ = plan(region, args.instance_id)
    if existing:
        if existing.get("instance_id") != args.instance_id:
            raise lab.LabError("The recorded W5 resources belong to a different EC2 instance.")
        proposed["vpc_id"] = existing.get("vpc_id", proposed["vpc_id"])
        proposed["instance_sg_id"] = existing.get("instance_sg_id", proposed["instance_sg_id"])
        proposed["subnets"] = [
            {"cidr": subnet["cidr"], "az": subnet["az"]}
            for subnet in existing.get("subnets", proposed["subnets"])
        ]
        proposed["route_table_name"] = existing.get("route_table_name", proposed["route_table_name"])
        proposed["db_security_group_name"] = existing.get("db_security_group_name", proposed["db_security_group_name"])
        proposed["db_subnet_group_name"] = existing.get(
            "db_subnet_group_name",
            existing.get("planned_db_subnet_group_name", proposed["db_subnet_group_name"]),
        )
        proposed["db_instance_identifier"] = existing.get(
            "db_instance_identifier",
            existing.get("planned_db_instance_identifier", proposed["db_instance_identifier"]),
        )
    expected_resource_check(region, proposed, existing)
    print(f"Identity verified: account ending {context['account'][-4:]}; region {region}.")
    print(f"EC2: {proposed['instance_id']} ({proposed['instance_state']}); VPC {proposed['vpc_id']}; EC2 subnet {proposed['subnet_id']}; source SG {proposed['instance_sg_id']}.")
    print(f"New private subnets: {proposed['subnets'][0]['cidr']} in {proposed['subnets'][0]['az']}, {proposed['subnets'][1]['cidr']} in {proposed['subnets'][1]['az']}.")
    print(f"New route table: {proposed['route_table_name']} (local route only); DB subnet group: {proposed['db_subnet_group_name']}.")
    print(f"New DB security group: {proposed['db_security_group_name']} (TCP 5432 source SG {proposed['instance_sg_id']} only).")
    print(f"RDS: {proposed['db_instance_identifier']}; PostgreSQL; db.t3.micro; gp3 20 GiB; encrypted; private; Single-AZ; database inspection.")
    print("Budget: one small RDS instance and 20 GiB storage; the service card's rough baseline is about $15/month outside eligible credits, with region-specific pricing; this script creates no billing budget or spending cap.")
    print("Exposure: no public RDS address, no IGW/NAT route in the new subnets, and inbound TCP 5432 only from the EC2 security group.")
    print("Cleanup: stop RDS for the W6 hold; later remove only ledgered W5 IDs (RDS, DB subnet group, DB SG/rule, route-table associations/table, then the two subnets). Do not remove EC2 or shared VPC resources.")
    if existing:
        print("W5 ledger already exists; resuming only resources recorded in .local/resources.json.")
        print("Recorded W5 keys: " + ", ".join(sorted(existing.keys())))
    if args.plan_only:
        return
    lab.approve("This operation creates/updates the exact W5 resources listed above. No IAM, NAT, public DB, or public ingress will be changed.", "yes")
    w5 = existing or {
        "instance_id": args.instance_id,
        "vpc_id": proposed["vpc_id"],
        "instance_sg_id": proposed["instance_sg_id"],
        "subnets": proposed["subnets"],
        "route_table_name": proposed["route_table_name"],
        "db_security_group_name": proposed["db_security_group_name"],
        "planned_db_subnet_group_name": proposed["db_subnet_group_name"],
        "planned_db_instance_identifier": proposed["db_instance_identifier"],
    }
    if w5.get("instance_id") != args.instance_id or w5.get("vpc_id") != proposed["vpc_id"]:
        raise lab.LabError("The recorded W5 resources belong to a different EC2/VPC; refusing to mix resource sets.")
    resources["w5"] = w5
    save_resources(resources)
    create_subnet(region, resources, w5, 0)
    create_subnet(region, resources, w5, 1)
    create_route_table(region, resources, w5)
    route_tables = aws(region, [
        "ec2", "describe-route-tables", "--route-table-ids", w5["route_table_id"],
    ]).get("RouteTables", [])
    routes = route_tables[0].get("Routes", []) if route_tables else []
    if len(routes) != 1 or routes[0].get("GatewayId") != "local":
        raise lab.LabError("The new route table did not read back as local-only; refusing subnet associations.")
    associate_subnets(region, resources, w5)
    create_database_security_group(region, resources, w5)
    allow_ec2_database_access(region, resources, w5)
    create_db_subnet_group(region, resources, w5)
    create_rds(region, resources, w5)
    wait_for_database(region, resources, w5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--plan-only", action="store_true", help="read-only; show the planned CIDRs, AZs and resource specs")
    args = parser.parse_args()
    try:
        run(args)
    except (lab.LabError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        message = str(exc) if isinstance(exc, lab.LabError) else type(exc).__name__
        print("STOP: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
