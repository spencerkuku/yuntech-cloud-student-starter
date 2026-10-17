#!/usr/bin/env python3
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from lab import run_aws, context, LabError, verify
from w5_db import (create_db_instance, load_or_create_password,
                   record_available_endpoint)

RES = ROOT / '.local' / 'resources.json'
CFG = ROOT / '.local' / 'config'
DBENV = ROOT / '.local' / 'db.env'
WEEK = 'w05'

def load_json(p):
    with open(p) as f: return json.load(f)
def save_json(p,d):
    with open(p,'w') as f:
        json.dump(d,f,indent=2); f.write('\n')

def load_config():
    values = {}
    for line in open(CFG, encoding='utf-8'):
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.strip().split('=', 1)
            values[key] = value
    for key in ('OWNER',):
        if not values.get(key):
            raise SystemExit(f'STOP: {key} missing in {CFG}')
    return values

def current_round(resources, region):
    if 'rounds' in resources:
        live = [r for r in resources['rounds']
                if r.get('instance', {}).get('state') != 'terminated']
        if not live:
            raise SystemExit('STOP: no live EC2 round in .local/resources.json')
        row = live[-1]
        if not row.get('subnet_id') or not row.get('security_group', {}).get('id'):
            raise SystemExit('STOP: live round has no subnet or host security group')
        return row

    from resources import deployment_target, ResourceLedgerError
    try:
        target = deployment_target(resources)
    except ResourceLedgerError as exc:
        raise SystemExit(f'STOP: {exc}') from None

    instance = describe_one(
        'ec2', ['describe-instances', '--instance-ids', target['instance_id']],
        region, 'Reservations'
    )
    if not instance or len(instance.get('Instances', [])) != 1:
        raise SystemExit('STOP: legacy EC2 instance not found in AWS')

    ec2 = instance['Instances'][0]
    groups = [sg['GroupId'] for sg in ec2.get('SecurityGroups', [])]
    sg_id = resources.get('sg_id')

    if (ec2.get('State', {}).get('Name') != target['state']
            or ec2.get('KeyName') != target['key_name']
            or not ec2.get('SubnetId')
            or not sg_id
            or sg_id not in groups):
        raise SystemExit('STOP: legacy EC2 details do not match AWS')

    return {
        'subnet_id': ec2['SubnetId'],
        'security_group': {'id': sg_id},
    }

def describe_one(service, args, region, key):
    result = run_aws([service, *args], region)
    rows = result.get(key, [])
    return rows[0] if rows else None


def find_existing_rds(region, identifier):
    result = run_aws(['rds', 'describe-db-instances'], region)
    return next(
        (db for db in result['DBInstances']
         if db['DBInstanceIdentifier'] == identifier),
        None
    )


def should_create_rds(w5_resources, aws_instance=None):
    if 'db_instance' in w5_resources:
        return False
    if aws_instance:
        raise SystemExit('STOP: RDS identifier exists but is not recorded in .local/resources.json; inspect ownership before retrying')
    return True

def recorded_rds_identifier(w5_resources, expected_identifier):
    db_instance = w5_resources.get('db_instance')
    if db_instance is None:
        return expected_identifier
    if not isinstance(db_instance, dict) or db_instance.get('identifier') != expected_identifier:
        raise SystemExit('STOP: recorded RDS identifier does not match OWNER; inspect .local/resources.json')
    return expected_identifier

def choose_private_subnets(region, vpc_id, used_cidrs, azs):
    existing = run_aws(['ec2', 'describe-subnets', '--filters', f'Name=vpc-id,Values={vpc_id}'], region)['Subnets']
    networks = [ipaddress.ip_network(item['CidrBlock']) for item in used_cidrs]
    candidates = []
    vpc = describe_one('ec2', ['describe-vpcs', '--vpc-ids', vpc_id], region, 'Vpcs')
    vpc_net = ipaddress.ip_network(vpc['CidrBlock'])
    for subnet in existing:
        networks.append(ipaddress.ip_network(subnet['CidrBlock']))
    if vpc_net.prefixlen > 24:
        raise SystemExit('STOP: VPC CIDR is too small to contain two /24 subnets')
    for candidate in vpc_net.subnets(new_prefix=24):
        if candidate.subnet_of(vpc_net) and not any(candidate.overlaps(item) for item in networks):
            candidates.append(candidate)
    available_azs = sorted({subnet['AvailabilityZone'] for subnet in existing} & set(azs))
    if len(available_azs) < 2:
        raise SystemExit('STOP: VPC does not have two usable AZs for private subnets')
    if len(candidates) < 2:
        raise SystemExit('STOP: could not find two non-overlapping /24 private subnet ranges')
    return [(str(candidates[0]), available_azs[0]), (str(candidates[1]), available_azs[1])]

def approve(owner, vpc_id, host_sg, cidrs, dbid):
    print('\n===== W5 resources to CREATE/USE =====')
    print(f'VPC: {vpc_id}; host SG source: {host_sg}; owner: {owner}')
    print(f'Private subnets: {cidrs[0][0]} ({cidrs[0][1]}), {cidrs[1][0]} ({cidrs[1][1]})')
    print(f'RDS: PostgreSQL db.t3.micro, 20 GiB gp3, encrypted, private, single AZ: {dbid}')
    print('Cost: two subnets/route table/SG plus RDS and 20 GiB storage while running.')
    print('Rollback: stop RDS/EC2 after verification; delete only IDs recorded in .local/resources.json.')
    if not sys.stdin.isatty():
        raise SystemExit('STOP: no interactive terminal. Review this plan and run it yourself.')
    if input("Type exactly 'CREATE' to proceed: ").strip() != 'CREATE':
        raise SystemExit('Cancelled; no AWS resources were changed.')

def tag(region, resource_id, owner):
    run_aws(['ec2', 'create-tags', '--resources', resource_id, '--tags', json.dumps([
        {'Key': 'course', 'Value': 'yuntech-115-1'}, {'Key': 'week', 'Value': WEEK},
        {'Key': 'owner', 'Value': owner}])], region)

def main():
    ctx = context(); region = ctx['region']
    verify(ctx)
    d=load_json(RES)
    owner = load_config()['OWNER']
    round_data = current_round(d, region)
    host_subnet = describe_one('ec2', ['describe-subnets', '--subnet-ids', round_data['subnet_id']], region, 'Subnets')
    vpc_id = host_subnet['VpcId']
    host_sg = round_data['security_group']['id']
    dbid = f'w05-{owner}-pg'
    d['w5'] = d.get('w5', {})

    existing = d['w5']
    dbid = recorded_rds_identifier(existing, dbid)
    approved = False

    def approve_if_needed(cidrs):
        nonlocal approved
        if not approved:
            approve(owner, vpc_id, host_sg, cidrs, dbid)
            approved = True

    if 'route_table' not in existing:
        used = [s['CidrBlock'] for s in run_aws(['ec2', 'describe-subnets', '--filters', f'Name=vpc-id,Values={vpc_id}'], region)['Subnets']]
        azs = [s['AvailabilityZone'] for s in run_aws(['ec2', 'describe-subnets', '--filters', f'Name=vpc-id,Values={vpc_id}'], region)['Subnets']]
        cidrs = choose_private_subnets(region, vpc_id, used, azs)
        approve_if_needed(cidrs)
        rt = run_aws(['ec2', 'create-route-table', '--vpc-id', vpc_id], region)['RouteTable']
        existing['route_table'] = {'id': rt['RouteTableId']}; save_json(RES, d); tag(region, rt['RouteTableId'], owner)
        for index, (cidr, az) in enumerate(cidrs, 1):
            subnet = run_aws(['ec2', 'create-subnet', '--vpc-id', vpc_id, '--cidr-block', cidr,
                              '--availability-zone', az], region)['Subnet']
            sid = subnet['SubnetId']
            run_aws(['ec2', 'associate-route-table', '--route-table-id', rt['RouteTableId'], '--subnet-id', sid], region)
            existing[f'private_subnet_{index}'] = {'id': sid, 'cidr': cidr, 'az': az}; save_json(RES, d); tag(region, sid, owner)
        print(f'created private subnets and route table {rt["RouteTableId"]}')
    else:
        if not all(key in existing for key in ('private_subnet_1', 'private_subnet_2')):
            raise SystemExit(
                'STOP: partial W5 subnet deployment recorded; '
                'inspect AWS resources before retrying'
            )
        cidrs = [(existing['private_subnet_1']['cidr'], existing['private_subnet_1']['az']),
                 (existing['private_subnet_2']['cidr'], existing['private_subnet_2']['az'])]

    if 'db_subnet_group' not in existing:
        approve_if_needed(cidrs)
        name = f'w05-{owner}-dbsg'
        run_aws(['rds', 'create-db-subnet-group', '--db-subnet-group-name', name,
                 '--db-subnet-group-description', 'W5 private inspection database',
                 '--subnet-ids', existing['private_subnet_1']['id'], existing['private_subnet_2']['id']], region)
        existing['db_subnet_group'] = {'name': name, 'subnets': [existing['private_subnet_1']['id'], existing['private_subnet_2']['id']]}; save_json(RES, d)

    if 'security_group_db' not in existing:
        approve_if_needed(cidrs)
        sg = run_aws(['ec2', 'create-security-group', '--group-name', f'w05-{owner}-db-sg',
                      '--description', 'W5 PostgreSQL from host SG only', '--vpc-id', vpc_id], region)['GroupId']
        run_aws(['ec2', 'authorize-security-group-ingress', '--group-id', sg, '--ip-permissions', json.dumps([{
            'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432,
            'UserIdGroupPairs': [{'GroupId': host_sg}]}])], region)
        existing['security_group_db'] = {'id': sg}; save_json(RES, d); tag(region, sg, owner)

    try:
        password = load_or_create_password(existing, DBENV)
    except ValueError as exc:
        raise SystemExit(f'STOP: {exc}') from None

    if should_create_rds(existing):
        already_exists = find_existing_rds(region, dbid)
        if not should_create_rds(existing, already_exists):
            raise SystemExit('STOP: unexpected existing RDS state; inspect resources.json before retrying')
        approve_if_needed(cidrs)
        parameters = {
            'DBInstanceIdentifier': dbid,
            'DBInstanceClass': 'db.t3.micro',
            'Engine': 'postgres',
            'AllocatedStorage': 20,
            'StorageType': 'gp3',
            'StorageEncrypted': True,
            'PubliclyAccessible': False,
            'MultiAZ': False,
            'DBName': 'inspection',
            'MasterUsername': 'inspection',
            'MasterUserPassword': password,
            'DBSubnetGroupName': existing['db_subnet_group']['name'],
            'VpcSecurityGroupIds': [existing['security_group_db']['id']],
            'BackupRetentionPeriod': 1,
        }
        with tempfile.TemporaryDirectory(prefix='.w5-rds-', dir=Path.home()) as request_dir:
            create_db_instance(run_aws, region, parameters, request_dir)
        existing['db_instance'] = {'identifier': dbid, 'status': 'creating', 'publicly_accessible': False}; save_json(RES, d)
        print(f'created RDS {dbid}; waiting for available')

    deadline = time.time() + 1800
    while time.time() < deadline:
        di = describe_one('rds', ['describe-db-instances', '--db-instance-identifier', dbid], region, 'DBInstances')
        if di:
            endpoint = di.get('Endpoint', {}).get('Address')
            existing['db_instance'] = {'identifier': dbid, 'address': endpoint,
                                       'status': di['DBInstanceStatus'],
                                       'publicly_accessible': di.get('PubliclyAccessible')}; save_json(RES, d)
            if record_available_endpoint(DBENV, di['DBInstanceStatus'],
                                         di.get('PubliclyAccessible'), endpoint,
                                         password):
                print('OK: available, PubliclyAccessible=false; updated .local/db.env mode 600')
                return
        time.sleep(30)
    raise SystemExit('STOP: timed out waiting for RDS available; inspect resources.json before retrying')

if __name__=='__main__':
    main()
