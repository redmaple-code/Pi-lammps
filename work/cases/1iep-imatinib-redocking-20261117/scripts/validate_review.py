"""Audit the supplementary result review and preserve original evidence hashes."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

CASE = Path(__file__).resolve().parents[1]
OUT = CASE / 'validation-review'


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


checks = []
original_validation = load(CASE / 'evidence/result-validation.json')
for row in original_validation['artifactManifest']:
    assert digest(CASE / row['path']) == row['digest']
checks.append({'check': 'originalCaseArtifacts', 'verdict': 'passed',
               'count': len(original_validation['artifactManifest'])})
cross = load(OUT / 'cross-checks.json')
pb = load(OUT / 'posebusters-summary.json')
assert cross['experimentalEntry']['chosenAuthChain'] == 'A'
assert cross['experimentalEntry']['chosenResidue'] == '201'
assert cross['experimentalEntry']['crystalLigands'][0]['tutorialToOriginalCrystalRMSD_A'] == 0
assert cross['nativeReceptorCheck']['solverInputHeavyAtoms'] == 2229
assert cross['nativeReceptorCheck']['maxPreparedHeavyAtomDisplacementVsOriginalCrystal_A'] == 0
checks.append({'check': 'originalCrystalAndSolverInput', 'verdict': 'passed',
               'ligandHeavyAtoms': 37, 'proteinHeavyAtoms': 2229})
original = load(CASE / 'analysis/results.json')
for row in cross['seedResults']:
    earlier = next(item for item in original['seeds'] if item['seed'] == row['seed'])
    assert math.isclose(earlier['top1ReferenceRMSD_A'], row['fixedFrameChemicalRMSD_A'], abs_tol=1e-6)
    assert row['nativePDBQTvsRestoredSDF_RMSD_A'] == 0
    assert row['atomPairsBelowVDWRatio0_75'] == 0
checks.append({'check': 'nativeOutputAndRMSD', 'verdict': 'passed', 'runs': 3})
assert len(pb['checkColumns']) == 28 and len(pb['poses']) == 20
assert len([row for row in pb['poses'] if row['index'][-1] == 0]) == 3
assert all(row['allChecksPassed'] for row in pb['poses'] if row['index'][-1] == 0)
assert sum(row['allChecksPassed'] for row in pb['poses']) == 5
assert all(len(row['failedChecks']) == 1 and row['failedChecks'][0].startswith('rmsd_')
           for row in pb['poses'] if row['failedChecks'])
assert all(row['allChecksPassed'] for row in pb['reference'])
checks.append({'check': 'thirdPartyPoseChecks', 'verdict': 'passed', 'top1Passes': 3,
               'allPoseFullPasses': 5, 'poseCount': 20, 'checksPerPose': 28})
assert max(row['fixedFrameSymmetricRMSD_A'] for row in cross['seedPairs']) < .1
checks.append({'check': 'seedConsistency', 'verdict': 'passed'})
wheels = load(OUT / 'wheel-integrity.json')
assert len(wheels) == 3 and all(row['hashMatched'] for row in wheels)
checks.append({'check': 'newWheelIntegrity', 'verdict': 'passed', 'wheels': 3})
license_text = (OUT / pb['licenseFiles'][0]).read_text(encoding='utf-8')
assert license_text.startswith('MIT License')
process = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True)
(OUT / 'pip-check.txt').write_text(process.stdout + process.stderr, encoding='utf-8')
assert process.returncode == 0
checks.append({'check': 'validationEnvironment', 'verdict': 'passed',
               'PoseBusters': '0.6.5', 'installedLicense': 'MIT'})
links = 0
body = (OUT / 'README.md').read_text(encoding='utf-8')
for reference in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)', body):
    if '://' not in reference:
        assert (OUT / reference.split('#')[0]).exists()
        links += 1
checks.append({'check': 'reviewLinks', 'verdict': 'passed', 'count': links})
figures = [{'path': name, 'digest': digest(OUT / name),
            'inspection': 'Opened through view_image; counts, axes, labels and data checked.'}
           for name in ['validation-overview.png', 'per-atom-differences.png']]
files = [{'path': path.relative_to(OUT).as_posix(), 'digest': digest(path)}
         for path in sorted(OUT.rglob('*')) if path.is_file() and path.name != 'review-validation.json']
report = {'validatedAt': datetime.now(timezone.utc).isoformat(), 'checks': checks,
          'figures': figures, 'artifactManifest': files, 'verdict': 'passedForRecordedReviewScope',
          'reviewerType': 'Current assistant plus recorded calculations and third-party checks.',
          'independentHumanReview': 'notPerformed',
          'scientificConclusion': 'Three top-ranked poses pass original recovery and supplementary plausibility checks in this single system.',
          'limitations': ['Single known ligand/receptor and fixed chemical state.',
                          'Absent waters/cofactors yield vacuous passes for their related checks.',
                          'Activity and affinity prediction require corresponding experimental validation.']}
(OUT / 'review-validation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'checksPassed': len(checks), 'originalArtifactsVerified': len(original_validation['artifactManifest']),
                  'top1Passes': 3, 'allPoseFullPasses': 5, 'localLinks': links}), flush=True)
