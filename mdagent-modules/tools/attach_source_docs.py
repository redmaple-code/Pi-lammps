"""Distribute handoff design documents without modifying their content."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT.parent/'source-snapshot/mdagent-handover-review-20261117/MDagent-subsystems-handover-20261117'
PACKAGES=ROOT/'packages/0.1.1'
upstream=json.loads((SOURCE/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))['files']
rows={n:{} for n in ('knowledge','evidence','gate','consistency','shared')}
for rel,meta in sorted(upstream.items()):
    if rel.startswith('docs/10_'): owner='knowledge'
    elif rel.startswith('docs/20_'): owner='evidence'
    elif rel.startswith('docs/30_'): owner='gate'
    elif rel.startswith('docs/40_'): owner='consistency'
    elif rel.startswith('docs/') or rel in ('README.md','交接手册.md','PACKAGING-NOTES.md','SANITIZATION.md','VERSION'): owner='shared'
    else: continue
    source=SOURCE/rel
    data=source.read_bytes()
    if hashlib.sha256(data).hexdigest()!=meta['sha256']: raise ValueError('document source changed: '+rel)
    target=PACKAGES/owner/'source-docs'/rel
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)
    rows[owner][rel]=meta['sha256']
for owner,files in rows.items():
    (PACKAGES/owner/'SOURCE-DOCS.json').write_text(json.dumps({'source':'handoff','files':files},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:len(v) for k,v in rows.items()},ensure_ascii=False))
