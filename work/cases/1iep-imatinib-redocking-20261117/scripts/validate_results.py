"""Audit real run provenance and arithmetic without launching another solver."""

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

CASE = Path(__file__).resolve().parents[1]


def load(relative):
    return json.loads((CASE / relative).read_text(encoding='utf-8'))


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


checks = []
downloads = load('evidence/downloads.json')
for item in downloads:
    assert hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256']
checks.append({'check': 'downloadHashes', 'result': 'passed', 'files': len(downloads)})
wheels = load('evidence/wheel-integrity.json')
assert len(wheels) == 21 and all(item['officialHashMatched'] for item in wheels)
checks.append({'check': 'officialPyPIWheelDigests', 'result': 'passed', 'wheels': len(wheels)})
assert 'No broken requirements found.' in (CASE / 'logs/pip-check.txt').read_text(encoding='utf-8-sig')
checks.append({'check': 'environmentDependencies', 'result': 'passed'})
plan = load('evidence/run-plan.json')
results = load('analysis/results.json')
assert len(plan['seeds']) == 3
for item in plan['inputs']:
    assert sha(CASE / item['path']) == item['digest']
checks.append({'check': 'frozenInputClosure', 'result': 'passed'})
run_ids = set()
for seed in plan['seeds']:
    receipt = load(f'runs/seed-{seed}/run.json')
    assert receipt['mode'] == 'real' and receipt['status'] == 'completed' and receipt['exitCode'] == 0
    assert receipt['inputs'] == plan['inputs']
    assert sha(CASE / f'runs/seed-{seed}/poses.pdbqt') == receipt['outputDigest']
    log = (CASE / f'runs/seed-{seed}/vina.log').read_text()
    assert f'random seed: {seed}' in log and 'Computing Vina grid ... done.' in log
    assert receipt['durationSeconds'] > 0
    run_ids.add(receipt['operationId'])
assert len(run_ids) == 3
checks.append({'check': 'realProcessAndNativeOutput', 'result': 'passed', 'completedRuns': 3})
coordinate = results['coordinateFrameCheck']
assert coordinate['commonReceptorHeavyAtoms'] == 2229
assert coordinate['maxSharedHeavyAtomDisplacement_A'] == 0
assert coordinate['missingPreparedAtomKeys'] == [] and coordinate['addedPreparedAtomKeys'] == []
checks.append({'check': 'receptorFramePreserved', 'result': 'passed', 'atoms': 2229})
maps = load('analysis/atom-maps.json')
assert len(maps) == 3
for mapping in maps:
    assert not mapping['coordinateRealignmentApplied']
    assert sorted(mapping['referenceIndexToPoseIndex']) == list(range(37))
    assert math.isclose(mapping['CalcRMS_A'], mapping['explicitRMSD_A'], abs_tol=1e-6)
checks.append({'check': 'independentAtomMappedRMSDFormula', 'result': 'passed', 'heavyAtoms': 37})
with (CASE / 'analysis/all-poses.csv').open(encoding='utf-8-sig', newline='') as stream:
    rows = list(csv.DictReader(stream))
assert len(rows) == results['poseCount']
for summary in results['seeds']:
    current = [row for row in rows if int(row['seed']) == summary['seed']]
    assert len(current) == summary['modeCount']
    assert int(current[0]['poseRank']) == 1
    assert math.isclose(float(current[0]['score_kcal_per_mol']), summary['top1Score_kcal_per_mol'])
    assert math.isclose(min(float(row['referenceHeavyAtomRMSD_A']) for row in current[:5]), summary['bestOf5ReferenceRMSD_A'])
assert results['top1Recovered'] == sum(row['top1ReferenceRMSD_A'] <= 2 for row in results['seeds'])
assert results['predeclaredPointTargetMet'] == (results['top1Recovered'] >= 2)
checks.append({'check': 'scoreAndCriterionAggregation', 'result': 'passed', 'poseCount': len(rows)})
links = []
for path in [CASE / 'README.md', CASE / 'OPEN_SOURCE_AUDIT.md', CASE / 'SIMULATION_SCHEME.md']:
    if not path.exists():
        continue
    body = re.sub(r'```.*?```', '', path.read_text(encoding='utf-8'), flags=re.S)
    for ref in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', body):
        if '://' in ref or ref.startswith('#'):
            continue
        target = path.parent / ref.split('#')[0]
        if target.name == 'result-validation.json':
            continue
        assert target.exists(), ref
        links.append(ref)
checks.append({'check': 'reportReferences', 'result': 'passed', 'checkedLinks': len(links)})
manifest = []
for path in sorted(CASE.rglob('*')):
    if path.is_file() and path.name != 'result-validation.json':
        manifest.append({'path': path.relative_to(CASE).as_posix(), 'digest': sha(path), 'bytes': path.stat().st_size})
receipt = {'validatedAt': datetime.now(timezone.utc).isoformat(), 'checks': checks,
           'scope': 'Deployment, actual execution provenance, fixed-frame RMSD, aggregation and report references.',
           'dataOrigin': 'realVinaRuns', 'independentScientificReview': 'notPerformed',
           'criteriaOutcome': results['predeclaredPointTargetMet'],
           'artifactManifest': manifest, 'verdict': 'passedForRecordedScope'}
(CASE / 'evidence/result-validation.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'checks': len(checks), 'files': len(manifest), 'actualRuns': 3,
                  'top1Recovered': results['top1Recovered'], 'poseCount': len(rows)}), flush=True)
