"""Read existing W9 receipt/photo endpoints; never create or resend student work."""
import argparse
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
DEVICE = re.compile(r'[A-Za-z0-9_-]{1,32}')
HASH = re.compile(r'[0-9a-f]{64}')

class CheckError(Exception):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise CheckError('服務回傳重新導向；請核對本組HTTPS入口。')

def load_config(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value,dict) or set(value)!={'api_base_url','expected_device_id','ca_file'}:
            raise ValueError()
        base=value['api_base_url'].rstrip('/')
        parts=urlsplit(base)
        if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path or parts.port not in (None,443)):
            raise ValueError()
        if parts.hostname.endswith('.invalid') or not DEVICE.fullmatch(value['expected_device_id']):
            raise ValueError()
        ca=(path.parent/value['ca_file']).resolve()
        if not ca.is_file():raise ValueError()
        return base,value['expected_device_id'],ca
    except (OSError,ValueError,TypeError,AttributeError):
        raise CheckError('設定需為本組HTTPS入口、有效device_id及可讀CA檔；範例值需修改。') from None

def token(name,prompt):
    value=os.environ.get(name) or getpass.getpass(prompt)
    if not value.isascii() or not 32<=len(value)<=512 or any(c.isspace() for c in value):
        raise CheckError('權杖格式不符；請透過受保護配置核對。')
    return value

def read_get(opener,url,secret,maximum):
    request=Request(url,headers={'Authorization':'Bearer '+secret},method='GET')
    try:
        with opener.open(request,timeout=10) as response:
            if response.status!=200:raise CheckError('服務未回傳成功狀態。')
            body=response.read(maximum+1)
            if len(body)>maximum:raise CheckError('回應超出本工具大小上限。')
            return {k.lower():v for k,v in response.headers.items()},body
    except HTTPError as exc:
        raise CheckError(f'API拒絕或尚未完成：HTTP {exc.code}；核對角色、事件及照片進度。') from None
    except (URLError,TimeoutError,ssl.SSLError,OSError):
        raise CheckError('連線或憑證驗證失敗；核對入口、CA、時間與目前服務。') from None

def validate_receipt(receipt,event_id,device_id):
    if not isinstance(receipt,dict):raise CheckError('收據格式不符。')
    if receipt.get('event_id')!=event_id or receipt.get('device_id')!=device_id:
        raise CheckError('收據的事件或裝置與預期不符。')
    if receipt.get('event_stored') is not True:
        raise CheckError('事件尚未確認入庫。')
    if receipt.get('photo_state')!='ready':raise CheckError('事件已存，照片尚未ready。')
    digest=receipt.get('photo_sha256')
    if not isinstance(digest,str) or not HASH.fullmatch(digest):raise CheckError('收據缺少有效照片摘要。')
    return digest

def validate_photo(headers,raw,expected):
    if headers.get('content-type','').split(';')[0].lower()!='image/jpeg':
        raise CheckError('照片回應不是JPEG。')
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=expected or headers.get('x-content-sha256')!=expected:
        raise CheckError('照片與收據／回應標頭的摘要不符。')
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise CheckError('請先安裝 tools/requirements-w09.txt。') from None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format!='JPEG' or image.width*image.height>4_000_000:raise ValueError()
            size=image.size
            image.load()
    except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):
        raise CheckError('照片無法完整解碼，或超出像素上限。') from None
    return size,digest

def check(opener,base,event_id,device_id,device_token,operator_token):
    headers,body=read_get(opener,base+'/w09/device/events/'+event_id,device_token,16*1024)
    if headers.get('content-type','').split(';')[0].lower()!='application/json':
        raise CheckError('收據回應不是JSON。')
    try:receipt=json.loads(body)
    except (ValueError,UnicodeError):raise CheckError('收據無法解析。') from None
    expected=validate_receipt(receipt,event_id,device_id)
    headers,raw=read_get(opener,base+'/w09/events/'+event_id+'/photo',operator_token,1024*1024)
    size,digest=validate_photo(headers,raw,expected)
    return raw,size,digest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('.local/w09-check.json'))
    parser.add_argument('--event-id',required=True)
    args=parser.parse_args()
    try:
        if not ID.fullmatch(args.event_id):raise CheckError('event_id格式不符。')
        base,device,ca=load_config(args.config)
        try:context=ssl.create_default_context(cafile=str(ca))
        except (OSError,ssl.SSLError):raise CheckError('CA檔無法建立信任設定。') from None
        opener=build_opener(HTTPSHandler(context=context),NoRedirect())
        device_secret=token('W09_DEVICE_TOKEN','本組device權杖（隱藏輸入）：')
        operator_secret=token('W09_OPERATOR_TOKEN','本組operator權杖（隱藏輸入）：')
        raw,size,digest=check(opener,base,args.event_id,device,device_secret,operator_secret)
        path=Path('.local/w09-check')/(args.event_id+'.jpg')
        try:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        except OSError:raise CheckError('照片已讀回，但本地預覽檔無法保存。') from None
        print('[OK] 雲端事件收據：ID與裝置正確，事件已存。')
        print(f'[OK] 同事件JPEG已授權讀回並核對摘要：{size[0]}x{size[1]}，{len(raw)} bytes。')
        print('照片預覽：'+str(path))
        print('[現場待查] 助教核對新事件／當次場景，並觀察持續警報沒有新增事件。')
        print('本工具不判定整組通過；沒有報告或工具輸出繳交。')
        return 0
    except CheckError as exc:
        print('[待處理] '+str(exc),file=sys.stderr);return 1
    except (EOFError,KeyboardInterrupt):
        print('[取消] 未完成查詢。',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
