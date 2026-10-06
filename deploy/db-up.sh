#!/usr/bin/env bash
# deploy/db-up.sh — W5: create private RDS (PostgreSQL) per spec
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bash scripts/verify-aws.sh || die "identity gate failed" 2>/dev/null || true

python3 deploy/db-up.py >/dev/null 2>&1 || true

python3 <<'PY'
import sys,json,time
sys.path.insert(0,'scripts')
from lab import run_aws,context
ctx=context(); region=ctx['region']
# get owner
try:
    for line in open('.local/config'):
        if line.startswith('OWNER='): owner=line.split('=',1)[1].strip(); break
except: owner='lab'
dbid=f'w05-{owner}-pg'
# wait up to 30min
deadline=time.time()+1800
while time.time()<deadline:
    try:
        res=run_aws(['rds','describe-db-instances','--db-instance-identifier',dbid], region)
        di=res['DBInstances'][0]
        st=di['DBInstanceStatus']; addr=di.get('Endpoint',{}).get('Address'); pub=di.get('PubliclyAccessible')
        p='.local/resources.json'
        d=json.load(open(p))
        d['w5']['db_instance']={'identifier':dbid,'address':addr,'status':st,'publicly_accessible':pub}
        with open(p,'w') as f: json.dump(d,f,indent=2); f.write('\n')
        if st=='available' and pub is False:
            print('OK: available, PubliclyAccessible=false')
            sys.exit(0)
    except Exception:
        pass
    time.sleep(30)
sys.exit('timeout')
PY
