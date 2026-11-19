"""Create or verify an inventory of package bytes (not a cryptographic signature)."""
import argparse
import hashlib
import json
from pathlib import Path

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['create','check'])
parser.add_argument('root',type=Path)
args=parser.parse_args()
root=args.root.resolve()
errors=[]
for name in ('shared','knowledge','evidence','gate','consistency'):
    folder=root/name; manifest=folder/'PACKAGE-INTEGRITY.json'
    files={p.relative_to(folder).as_posix():digest(p) for p in folder.rglob('*') if p.is_file()
           and p != manifest and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if args.action == 'create':
        manifest.write_text(json.dumps({'algorithm':'sha256','files':dict(sorted(files.items()))},indent=2)+'\n',encoding='utf-8')
    else:
        expected=json.loads(manifest.read_text(encoding='utf-8'))['files']
        missing=sorted(set(expected)-set(files)); extra=sorted(set(files)-set(expected))
        changed=sorted(p for p in set(files)&set(expected) if files[p]!=expected[p])
        if missing or extra or changed: errors.append({'module':name,'missing':missing,'extra':extra,'changed':changed})
    print(name,len(files),'files')
print(json.dumps({'ok':not errors,'errors':errors},ensure_ascii=False))
raise SystemExit(bool(errors))
