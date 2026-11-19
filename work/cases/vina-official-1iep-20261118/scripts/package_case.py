"""Create a verified archive only after the evidence audit succeeds."""

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

CASE = Path(__file__).resolve().parents[1]
WORKSPACE = CASE.parents[2]
validation = json.loads((CASE / 'evidence/case-validation.json').read_text(encoding='utf-8'))
if not validation['auditPassed']:
    raise RuntimeError('Resolve evidence-audit failures before packaging.')
index = json.loads((CASE / 'artifact-index.json').read_text(encoding='utf-8'))
for item in index['artifacts']:
    digest = 'sha256:' + hashlib.sha256((CASE / item['path']).read_bytes()).hexdigest()
    if digest != item['digest']:
        raise RuntimeError('Artifact changed after audit: ' + item['path'])
destination = WORKSPACE / 'deliverables'
destination.mkdir(exist_ok=True)
archive = destination / '官方1IEP案例复现-20261118.zip'
files = [p for p in sorted(CASE.rglob('*')) if p.is_file() and '__pycache__' not in p.parts]
with ZipFile(archive, 'w', ZIP_DEFLATED) as bundle:
    for file in files:
        bundle.write(file, (Path(CASE.name) / file.relative_to(CASE)).as_posix())
with ZipFile(archive) as bundle:
    if bundle.testzip() is not None:
        raise RuntimeError('ZIP CRC validation failed.')
    for file in files:
        name = (Path(CASE.name) / file.relative_to(CASE)).as_posix()
        if hashlib.sha256(bundle.read(name)).digest() != hashlib.sha256(file.read_bytes()).digest():
            raise RuntimeError('Archive readback mismatch: ' + name)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='utf-8')
receipt = {'archive': archive.name, 'bytes': archive.stat().st_size, 'sha256': digest,
    'files': len(files), 'CRCVerified': True, 'allFilesReadbackVerified': True,
    'evidenceAuditPassed': validation['auditPassed'], 'checksPassed': validation['passed']}
archive.with_suffix('.zip.receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(receipt, ensure_ascii=True), flush=True)
