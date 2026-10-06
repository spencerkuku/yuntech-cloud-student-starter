#!/usr/bin/env python3
import sys,json,ipaddress,secrets,string,os
sys.path.insert(0,'scripts')
from lab import run_aws,context,LabError

ROOT = os.path.abspath('.')
RES = os.path.join(ROOT,'.local','resources.json')
CFG = os.path.join(ROOT,'.local','config')
DBENV = os.path.join(ROOT,'.local','db.env')

def load_json(p):
    with open(p) as f: return json.load(f)
def save_json(p,d):
    with open(p,'w') as f:
        json.dump(d,f,indent=2); f.write('\n')

def main():
    ctx=context(); region=ctx['region']
    d=load_json(RES)
    owner=''
    try:
        for line in open(CFG):
            if line.startswith('OWNER='): owner=line.split('=',1)[1].strip(); break
    except: pass
    if not owner: owner='lab'
    d['w5']=d.get('w5',{})
    # skip if already available
    dbid=f'w05-{owner}-pg'
    try:
        res=run_aws(['rds','describe-db-instances','--db-instance-identifier',dbid], region)
        di=res['DBInstances'][0]
        d['w5']['db_instance']=d['w5'].get('db_instance',{})
        d['w5']['db_instance']['identifier']=dbid
        d['w5']['db_instance']['address']=di.get('Endpoint',{}).get('Address')
        d['w5']['db_instance']['status']=di['DBInstanceStatus']
        save_json(RES,d)
        print('exists', di['DBInstanceStatus'], di.get('PubliclyAccessible'))
        return
    except Exception as e:
        pass  # create

if __name__=='__main__':
    main()
