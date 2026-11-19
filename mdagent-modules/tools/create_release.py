"""Create individual module archives and a single distributable bundle."""
import hashlib
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PACKAGES=ROOT/'packages/0.1.1'
OUT=ROOT/'releases/0.1.1'
OUT.mkdir(parents=True,exist_ok=False)

def add_tree(z,base,prefix):
    for path in sorted(base.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo'): continue
        z.write(path,(Path(prefix)/path.relative_to(base)).as_posix())

def finish(path):
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix+'.sha256').write_text(digest+'  '+path.name+'\n',encoding='ascii')
    print(path.name,digest)

for name in ('shared','knowledge','evidence','gate','consistency'):
    target=OUT/f'mdagent-{name}-0.1.1.zip'
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        add_tree(z,PACKAGES/name,name)
    finish(target)

target=OUT/'mdagent-modules-0.1.1.zip'
with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for file in ('README.md','AUDIT-AND-VERIFICATION.md'):
        z.write(ROOT/file,'mdagent-modules/'+file)
    add_tree(z,PACKAGES,'mdagent-modules/packages/0.1.1')
    for file in ('split_handover.py','package_integrity.py','verify_modules.py','verify_mcp.py','extract_interfaces.py'):
        z.write(ROOT/'tools'/file,'mdagent-modules/tools/'+file)
    for name in ('0.1.1','0.1.1-retry'):
        add_tree(z,ROOT/'evidence'/name,'mdagent-modules/evidence/'+name)
finish(target)
