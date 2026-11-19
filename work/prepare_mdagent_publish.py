"""Select the two handoff sets and scan before writing to a publish checkout."""
import hashlib
import io
from pathlib import Path
import re
import shutil
import sys
import zipfile

WORK=Path(__file__).resolve().parents[1]
CHECKOUT=WORK/'work/mdagent-github-publish-20261118'
DEST=CHECKOUT/'subsystems'
if DEST.exists(): raise SystemExit('publish destination already exists; refusing to overwrite')
inventory=Path(r'D:\personal-twin\inventory\audit-tokens.txt')
if not inventory.is_file(): raise SystemExit('private data audit inventory unavailable')
tokens=[x.strip().encode('utf-8') for x in inventory.read_text(encoding='utf-8-sig').splitlines()
        if x.strip() and not x.lstrip().startswith('#') and len(x.strip())>=6]
if not tokens: raise SystemExit('private data audit inventory empty')
patterns=[re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
          re.compile(rb'(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{30,255}(?![A-Za-z0-9_])'),
          re.compile(rb'(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{32,255}(?![A-Za-z0-9_])'),
          re.compile(rb'(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])')]

def inventory_tree(name):
    folder=WORK/name
    components=['README.md','AUDIT-AND-VERIFICATION.md','docs','tools','tests','packages/'+('0.1.1' if name=='mdagent-modules' else '0.2.0')]
    paths=[]
    for component in components:
        source=folder/component
        if not source.exists(): continue
        for file in ([source] if source.is_file() else source.rglob('*')):
            if file.is_file() and '__pycache__' not in file.parts and file.suffix not in ('.pyc','.pyo'):
                paths.append((file,Path(name)/file.relative_to(folder)))
    release=folder/'releases'/('0.1.1' if name=='mdagent-modules' else '0.2.0')
    for file in release.iterdir():
        if file.is_file(): paths.append((file,Path(name)/'downloads'/file.name))
    # Include actual validation results; logs are fixture-based and never contain production inputs.
    ev=folder/'evidence'
    names=['0.1.1','0.1.1-retry'] if name=='mdagent-modules' else ['0.2.0']
    for version in names:
        for file in (ev/version).rglob('*'):
            if file.is_file() and file.suffix in ('.json','.log'):
                paths.append((file,Path(name)/'evidence'/version/file.relative_to(ev/version)))
    return paths

files=inventory_tree('mdagent-modules')+inventory_tree('mdagent-standalone')
private_hits=[]; credential_hits=[]
def check(label,data,depth=0):
    if any(t in data for t in tokens): private_hits.append(label)
    if any(p.search(data) for p in patterns): credential_hits.append(label)
    if depth<2 and data[:4]==b'PK\x03\x04':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for item in z.infolist():
                    if item.is_dir(): continue
                    if item.file_size>40*1024*1024: raise ValueError('archive member too large')
                    check(label+'!'+item.filename,z.read(item),depth+1)
        except zipfile.BadZipFile: raise ValueError('invalid zip: '+label)
for source,relative in files: check(relative.as_posix(),source.read_bytes())
print('Selected files:',len(files))
print('Personal-data inventory matches:',len(private_hits))
print('Credential-pattern matches:',len(credential_hits))
if private_hits or credential_hits:
    print('Publication stopped; matching locations (contents and values suppressed):')
    for name in sorted(set(private_hits+credential_hits)): print(name)
    sys.exit(2)
for source,relative in files:
    target=DEST/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source,target)
print('Copied validated files to isolated checkout')
