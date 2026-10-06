#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / '.local' / 'resources.json'
SECRET = ROOT / '.local' / 'app.env'

def get_pubip():
    try:
        d = json.load(open(RES))
        ip = d['rounds'][-1].get('public_ip')
        if not ip:
            # maybe running state
            pass
        return ip
    except Exception:
        return None

def get_tokens():
    tok = {}
    try:
        for line in open(SECRET):
            line = line.strip()
            if not line or '=' not in line: continue
            k,v = line.split('=',1)
            tok[k]=v
    except Exception:
        pass
    return tok['REPORTER_TOKEN'], tok['OPERATOR_TOKEN']

def curl_json(url, method='GET', headers=None, data=None, timeout=10):
    cmd = ['curl','-sS','--max-time',str(timeout),'-w','|%{http_code}']
    if method=='POST':
        cmd += ['-X','POST','-H','Content-Type: application/json']
    if headers:
        for h,v in headers.items():
            cmd += ['-H',f'{h}: {v}']
    if data is not None:
        cmd += ['-d', json.dumps(data)]
    cmd += [url]
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stdout
    if '|' in out:
        body,code = out.rsplit('|',1)
    else:
        body,code = out,'000'
    try:
        j = json.loads(body) if body else {}
    except Exception:
        j = body
    return int(code) if code.isdigit() else code, j

def main():
    ip = get_pubip()
    if not ip:
        print('no public ip'); sys.exit(1)
    base = f'http://{ip}'
    rpt, op = get_tokens()
    # health
    code,h = curl_json(f'{base}/health')
    print(f"health: version={h.get('version') if isinstance(h,dict) else ''} db_configured={h.get('db_configured') if isinstance(h,dict) else ''}")
    # gen event id
    import uuid
    eid = f'test-{uuid.uuid4().hex[:16]}'
    evt = {
        'event_id': eid,
        'device_id': 'dev-001',
        'observed_at': '2026-10-06T10:00:00Z',
        'type': 'status',
        'note': 'hello'
    }
    # 1
    c1,r1 = curl_json(f'{base}/events','POST',headers={'Authorization':f'Bearer {rpt}'}, data=evt)
    print(f"#1 POST new -> {c1} {r1}")
    # 2
    c2,r2 = curl_json(f'{base}/events','POST',headers={'Authorization':f'Bearer {rpt}'}, data=evt)
    print(f"#2 POST same -> {c2} {r2}")
    # 3
    evt3 = dict(evt); evt3['note']='changed'
    c3,r3 = curl_json(f'{base}/events','POST',headers={'Authorization':f'Bearer {rpt}'}, data=evt3)
    print(f"#3 POST conflict -> {c3} {r3}")
    # 4 restart inspection (via ssh) - but we run from codespace; skip local, print instruction? but spec says matrix script runs and #4 is "sudo systemctl restart inspection 後查 #1" - script can do ssh
    # get key
    # try to ssh
    try:
        # find key
        import glob
        key=None
        for k in glob.glob(str(Path.home()/'.ssh'/'w03-*-key')):
            key=k; break
        if key:
            ssh = ['ssh','-i',key,'-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=10','ec2-user@'+ip,'sudo systemctl restart inspection']
            subprocess.run(ssh, timeout=30)
    except Exception as e:
        print('note: could not ssh restart', e)
    # query #1
    c4,r4 = curl_json(f'{base}/events/'+eid, headers={'Authorization':f'Bearer {op}'})
    print(f"#4 GET after restart -> {c4} {r4}")
    # 5 psql count from EC2
    cnt='?'
    try:
        if key:
            # read db env values
            dbenv = ROOT / '.local' / 'db.env'
            envs={}
            for line in open(dbenv):
                if '=' in line: k,v=line.strip().split('=',1); envs[k]=v
            # build psql
            sh = f"set -a; . /etc/inspection/app.env; set +a; PGPASSWORD=\"$DB_PASSWORD\" psql \"host=$DB_HOST dbname=$DB_NAME user=$DB_USER sslmode=verify-full sslrootcert=/etc/inspection/rds-ca.pem\" -v event_id='{eid}' -tAc \"SELECT count(*) FROM events WHERE event_id=:'event_id';\" 2>&1 | head -1"
            ssh2 = ['ssh','-i',key,'-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=15','ec2-user@'+ip,"sudo bash -c %s" % repr(sh)]
            p2 = subprocess.run(ssh2, capture_output=True, text=True, timeout=60)
            cnt = p2.stdout.strip().splitlines()[-1] if p2.stdout else p2.stderr.strip()
    except Exception as e:
        cnt = f'err:{e}'
    print(f"#5 psql count(event_id={eid}) -> {cnt}")

if __name__=='__main__':
    main()
