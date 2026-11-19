"""Validate implementation references without treating module evidence as acceptance."""
import json
import re
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
modules = json.loads((ROOT / 'module-status.json').read_text(encoding='utf-8'))['modules']
requirements = json.loads((ROOT / 'requirements-status.json').read_text(encoding='utf-8'))['requirements']
planning = json.loads((ROOT.parent / 'module-development/requirements.json').read_text(encoding='utf-8'))['requirements']
assert len(modules) == 21
assert {row['requirementId'] for row in requirements} == {row['requirementId'] for row in planning}
assert len(requirements) == 116
assert len({row['requirementId'] for row in requirements}) == 116
module_ids = {row['moduleId'] for row in modules}
for row in modules:
    assert row['implemented'] and row['remaining']
    for ref in row['implementationRefs'] + row['verificationRefs']:
        assert (ROOT / ref).is_file(), ref
for row in requirements:
    assert row['moduleId'] in module_ids
    assert row['sourceRow'].startswith('| ' + row['requirementId'])
    assert row['requirementSpecificAcceptance'] == 'notFullyExecuted'
    assert row['productAcceptance'] is None
links = 0
for path in [*ROOT.glob('*.md'), ROOT.parents[1] / 'simulation/README.md']:
    text = path.read_text(encoding='utf-8')
    assert text.count('```') % 2 == 0, path
    for ref in re.findall(r'\]\(([^)]+)\)', text):
        if ref.startswith(('https://', 'http://', '#')):
            continue
        assert (path.parent / ref.split('#', 1)[0]).exists(), (path, ref)
        links += 1
snapshot = ROOT.parents[1] / 'simulation/work/delivery-final'
manifest = json.loads((snapshot / 'manifest.json').read_text(encoding='utf-8'))
assert manifest['databaseIntegrity'] == 'ok' and manifest['pendingOperations'] == 0
for item in manifest['exportedFiles']:
    actual = hashlib.sha256((snapshot / item['path']).read_bytes()).hexdigest()
    assert item['digest'] == 'sha256:' + actual, item['path']
sources = json.loads((snapshot / 'source-manifest.json').read_text(encoding='utf-8'))['sourceFiles']
for item in sources:
    actual = hashlib.sha256((ROOT.parents[1] / 'simulation' / item['path']).read_bytes()).hexdigest()
    assert item['digest'] == 'sha256:' + actual, item['path']
print(json.dumps({'modules': len(modules), 'requirements': len(requirements),
                  'checkedLinks': links, 'snapshotIntegrity': 'ok',
                  'acceptance': 'partial'}, ensure_ascii=False))
