"""Check every packaged byte against its own recorded inventory."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
errors=[]
for name in ('knowledge','evidence','gate','consistency'):
    root=ROOT/'packages/0.2.0'/name
    expected=json.loads((root/'SHA256SUMS.json').read_text(encoding='utf-8'))
    actual={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and p.name!='SHA256SUMS.json'
            and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')}
    issues=[key for key in sorted(set(actual)|set(expected)) if actual.get(key)!=expected.get(key)]
    print(name,len(actual),'files','OK' if not issues else 'FAIL')
    errors.extend(name+':'+key for key in issues)
if errors: print('\n'.join(errors))
sys.exit(bool(errors))
