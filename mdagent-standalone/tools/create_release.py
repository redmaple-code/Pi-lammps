"""Build individual standalone archives and one full bundle without caches."""
import hashlib
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PKGS=ROOT/'packages/0.2.0'
OUT=ROOT/'releases/0.2.0'
OUT.mkdir(parents=True,exist_ok=False)
def add_tree(z,root,prefix):
    for path in sorted(root.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc','.pyo'):
            z.write(path,(Path(prefix)/path.relative_to(root)).as_posix())
def done(path):
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix+'.sha256').write_text(checksum+'  '+path.name+'\n',encoding='ascii')
    print(path.name,checksum)
for name in ('knowledge','evidence','gate','consistency'):
    target=OUT/('mdagent-standalone-'+name+'-0.2.0.zip')
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=9) as z: add_tree(z,PKGS/name,name)
    done(target)
target=OUT/'mdagent-standalone-0.2.0.zip'
with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for file in ('README.md','AUDIT-AND-VERIFICATION.md'):
        z.write(ROOT/file,'mdagent-standalone/'+file)
    add_tree(z,PKGS,'mdagent-standalone/packages/0.2.0')
    for folder in ('docs','tools','tests','evidence/0.2.0'):
        add_tree(z,ROOT/folder,'mdagent-standalone/'+folder)
done(target)
