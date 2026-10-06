#!/usr/bin/env python3
import ipaddress
import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import sys
import time

sys.path.insert(0,'scripts')
from lab import run_aws, context, LabError, verify

ROOT = os.path.abspath('.')
RES = os.path.join(ROOT,'.local','resources.json')
CFG = os.path.join(ROOT,'.local','config')
DBENV = os.path.join(ROOT,'.local','db.env')
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

def write_db_env(host, password):
    path = Path(DBENV)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = f'DB_HOST={host}\nDB_NAME=inspection\nDB_USER=inspection\nDB_PASSWORD={password}\n'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

def current_round(resources):
    live = [r for r in resources.get('rounds', [])
            if r.get('instance', {}).get('state') != 'terminated']
    if not live:
        raise SystemExit('STOP: no live EC2 round in .local/resources.json')
    row = live[-1]
    if not row.get('subnet_id') or not row.get('security_group', {}).get('id'):
        raise SystemExit('STOP: live round has no subnet or host security group')
    return row

def describe_one(service, args, region, key):
    result = run_aws([service, *args], region)
    rows = result.get(key, [])
    return rows[0] if rows else None

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
    round_data = current_round(d)
    host_subnet = describe_one('ec2', ['describe-subnets', '--subnet-ids', round_data['subnet_id']], region, 'Subnets')
    vpc_id = host_subnet['VpcId']
    host_sg = round_data['security_group']['id']
    dbid = f'w05-{owner}-pg'
    d['w5'] = d.get('w5', {})

    existing = d['w5']
    if 'route_table' not in existing:
        used = [s['CidrBlock'] for s in run_aws(['ec2', 'describe-subnets', '--filters', f'Name=vpc-id,Values={vpc_id}'], region)['Subnets']]
        azs = [s['AvailabilityZone'] for s in run_aws(['ec2', 'describe-subnets', '--filters', f'Name=vpc-id,Values={vpc_id}'], region)['Subnets']]
        cidrs = choose_private_subnets(region, vpc_id, used, azs)
        approve(owner, vpc_id, host_sg, cidrs, dbid)
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
        cidrs = [(existing['private_subnet_1']['cidr'], existing['private_subnet_1']['az']),
                 (existing['private_subnet_2']['cidr'], existing['private_subnet_2']['az'])]

    if 'db_subnet_group' not in existing:
        name = f'w05-{owner}-dbsg'
        run_aws(['rds', 'create-db-subnet-group', '--db-subnet-group-name', name,
                 '--db-subnet-group-description', 'W5 private inspection database',
                 '--subnet-ids', existing['private_subnet_1']['id'], existing['private_subnet_2']['id']], region)
        existing['db_subnet_group'] = {'name': name, 'subnets': [existing['private_subnet_1']['id'], existing['private_subnet_2']['id']]}; save_json(RES, d)

    if 'security_group_db' not in existing:
        sg = run_aws(['ec2', 'create-security-group', '--group-name', f'w05-{owner}-db-sg',
                      '--description', 'W5 PostgreSQL from host SG only', '--vpc-id', vpc_id], region)['GroupId']
        run_aws(['ec2', 'authorize-security-group-ingress', '--group-id', sg, '--ip-permissions', json.dumps([{
            'IpProtocol': 'tcp', 'FromPort': 5432, 'ToPort': 5432,
            'UserIdGroupPairs': [{'GroupId': host_sg}]}])], region)
        existing['security_group_db'] = {'id': sg}; save_json(RES, d); tag(region, sg, owner)

    db_secret = Path(DBENV)
    password = None
    if db_secret.exists():
        if (db_secret.stat().st_mode & 0o777) != 0o600:
            raise SystemExit(f'STOP: {DBENV} must be mode 600')
    else:
        password = secrets.token_urlsafe(32)

    if 'db_instance' not in existing:
        if password is None:
            raise SystemExit(f'STOP: {DBENV} exists but is unreadable by this workflow; preserve it and inspect manually')
        run_aws(['rds', 'create-db-instance', '--db-instance-identifier', dbid,
                 '--db-instance-class', 'db.t3.micro', '--engine', 'postgres',
                 '--allocated-storage', '20', '--storage-type', 'gp3', '--storage-encrypted',
                 '--no-publicly-accessible', '--no-multi-az', '--db-name', 'inspection',
                 '--master-username', 'inspection', '--master-user-password', password,
                 '--db-subnet-group-name', existing['db_subnet_group']['name'],
                 '--vpc-security-group-ids', existing['security_group_db']['id'],
                 '--backup-retention-period', '1'], region)
        existing['db_instance'] = {'identifier': dbid, 'status': 'creating', 'publicly_accessible': False}; save_json(RES, d)
        write_db_env('pending', password)
        print(f'created RDS {dbid}; waiting for available')

    deadline = time.time() + 1800
    while time.time() < deadline:
        di = describe_one('rds', ['describe-db-instances', '--db-instance-identifier', dbid], region, 'DBInstances')
        if di:
            endpoint = di.get('Endpoint', {}).get('Address')
            existing['db_instance'] = {'identifier': dbid, 'address': endpoint,
                                       'status': di['DBInstanceStatus'],
                                       'publicly_accessible': di.get('PubliclyAccessible')}; save_json(RES, d)
            if di['DBInstanceStatus'] == 'available' and di.get('PubliclyAccessible') is False and endpoint:
                if db_secret.exists():
                    print('OK: available, PubliclyAccessible=false; existing db.env preserved')
                else:
                    write_db_env(endpoint, password)
                    print('OK: available, PubliclyAccessible=false; wrote .local/db.env mode 600')
                return
        time.sleep(30)
    raise SystemExit('STOP: timed out waiting for RDS available; inspect resources.json before retrying')

if __name__=='__main__':
    main()
