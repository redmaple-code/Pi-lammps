"""Check audit references and create an integrity manifest for this audit revision."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
DOCS = WORKSPACE / 'docs/audit/2026-11-17'
links = 0
for path in DOCS.glob('*.md'):
    text = path.read_text(encoding='utf-8')
    assert text.count('```') % 2 == 0
    for target in re.findall(r'\]\(([^)]+)\)', text):
        if target.startswith(('http://', 'https://', '#')):
            continue
        assert (path.parent / target).exists(), (path, target)
        links += 1
entries = []
for line in (DOCS / 'AI工作错误清单.md').read_text(encoding='utf-8').splitlines():
    if not re.match(r'^\| AI-\d{3} \|', line):
        continue
    values = [field.strip() for field in line.strip('|').split('|')]
    entries.append({'id': values[0], 'finding': values[1], 'impact': values[2], 'statusAndEvidence': values[3]})
assert [row['id'] for row in entries] == [f'AI-{i:03d}' for i in range(1, 41)]
(ROOT / 'findings.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
control_text = (ROOT / 'no-tool-control/log.lammps').read_text(encoding='utf-8')
control_receipt = json.loads((ROOT / 'no-tool-control/receipt.json').read_text(encoding='utf-8'))
assert control_receipt['exitCode'] == 0
assert not re.search(r'^(ERROR|WARNING)\b', control_text, re.M)
assert 'REDMAPLE_GRINDING_COMPLETE' in control_text
main = json.loads((ROOT / 'main-case-audit.json').read_text(encoding='utf-8'))
assert main['identity']['idsMatch'] and main['identity']['typesMatch']
comparison = json.loads((ROOT / 'control-comparison.json').read_text(encoding='utf-8'))
assert comparison['initialStateMatches']
originals = json.loads((ROOT / 'original-output-integrity.json').read_text(encoding='utf-8'))
assert all(row['matchesArchivedArtifact'] for row in originals)
files = []
for path in sorted([*ROOT.rglob('*'), *DOCS.glob('*.md')]):
    if not path.is_file() or '__pycache__' in path.parts or path.name == 'manifest.json':
        continue
    files.append({'path': str(path.relative_to(WORKSPACE)).replace('\\', '/'),
                  'byteSha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size})
manifest = {'revision': 'AUDIT-1', 'createdAt': datetime.now(timezone.utc).isoformat(),
            'mainCaseId': 'hea-grinding-8b2245c6-ab13-4ef0-be68-85483edce49d',
            'findings': len(entries), 'checkedLocalLinks': links, 'originalOutputFilesVerified': len(originals),
            'controlStatus': control_receipt['status'], 'scientificValidation': 'partial',
            'experimentalConsistency': 'unverified',
            'activeConfirmedImplementationProblems': ['AI-001', 'AI-002'], 'files': files}
(ROOT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key: value for key, value in manifest.items() if key != 'files'}))
