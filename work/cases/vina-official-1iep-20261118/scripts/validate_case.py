"""Audit provenance and comparison consistency without forcing scientific success."""

from datetime import datetime, timezone
import copy
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

import numpy as np
from jsonschema import Draft202012Validator
from rdkit import Chem
from rdkit.Chem import rdMolAlign
from scipy.optimize import linear_sum_assignment
from meeko import PDBQTMolecule, RDKitMolCreate

CASE = Path(__file__).resolve().parents[1]
checks = []


def read(relative):
    return json.loads((CASE / relative).read_text(encoding='utf-8'))


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def rows(relative):
    with (CASE / relative).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def check(name, condition, evidence):
    checks.append({'check': name, 'passed': bool(condition), 'evidence': evidence})


def close(a, b):
    return abs(float(a) - float(b)) < 1e-8


def rmsd(a, b):
    return rdMolAlign.CalcRMS(a, b, maxMatches=1000000,
        symmetrizeConjugatedTerminalGroups=True)


def poses(relative):
    return list(Chem.SDMolSupplier(str(CASE / relative), removeHs=True))


def native_poses(relative):
    path = CASE / relative
    restored = [m for m in RDKitMolCreate.from_pdbqt_mol(
        PDBQTMolecule.from_file(str(path), skip_typing=True)) if m is not None]
    if len(restored) != 1:
        raise RuntimeError('Native output must contain one chemical identity.')
    molecule = Chem.RemoveHs(restored[0])
    scores = [float(x) for x in re.findall(r'REMARK VINA RESULT:\s+([-\d.]+)', path.read_text())]
    result = []
    for conformer, score in zip(molecule.GetConformers(), scores):
        pose = Chem.Mol(molecule)
        pose.RemoveAllConformers()
        pose.AddConformer(Chem.Conformer(conformer), assignId=True)
        pose.SetDoubleProp('VinaScore_kcal_per_mol', score)
        result.append(pose)
    if len(result) != len(scores):
        raise RuntimeError('Native score/pose count mismatch.')
    return result


def export_matches(native, exported):
    # V2000 SDF stores four decimal places. This is a serialization bound,
    # separate from the unchanged 2 A scientific recovery criterion.
    return len(native) == len(exported) and all(b is not None
        and Chem.MolToSmiles(a) == Chem.MolToSmiles(b)
        and np.max(np.abs(a.GetConformer().GetPositions() - b.GetConformer().GetPositions())) <= .0001
        for a, b in zip(native, exported))


source = read('sources/source-lock.json')
plan = read('evidence/run-plan.json')
metrics = read('analysis/metrics.json')
for item in source['files']:
    check('source hash: ' + item['localPath'], sha(CASE / item['localPath']) == 'sha256:' + item['sha256'],
          'sources/source-lock.json')
check('pinned source commit', source['commit'] == plan['sourceCommit'] ==
      '8eb40404f4f45608acb3b01427587ac049f27c1f', 'sources/source-lock.json')
for item in plan['officialInputs']:
    check('bound solver input: ' + item['path'], sha(CASE / item['path']) == item['digest'],
          'evidence/run-plan.json')

reference = native_poses('reference/official-vina-output.pdbqt')
check('reference SDF export preserves native coordinates at stored precision',
      export_matches(reference, poses('reference/official-output.sdf')), 'reference/official-output.sdf')
crystal = poses('reference/crystal-ligand.sdf')[0]
identity = Chem.MolToSmiles(crystal)
check('crystal chemical state', crystal.GetNumHeavyAtoms() == 37 and Chem.GetFormalCharge(crystal) == 1,
      'reference/crystal-ligand.sdf')
receptor_audit = read('evidence/receptor-analysis-input-audit.json')
check('analysis receptor matches solver receptor', receptor_audit['heavyAtomIdentityAndCoordinatesExactMatch']
      and receptor_audit['solverHeavyAtoms'] == receptor_audit['geometryCheckHeavyAtoms'] == 2229
      and receptor_audit['rdkitParsingSucceeded'], 'evidence/receptor-analysis-input-audit.json')
reference_scores = [float(x) for x in re.findall(r'REMARK VINA RESULT:\s+([-\d.]+)',
    (CASE / 'reference/official-vina-output.pdbqt').read_text())]
check('official four scores', reference_scores == [-13.234, -11.293, -11.281, -11.146]
      and len(reference) == 4, 'reference/official-vina-output.pdbqt')
translated = Chem.Mol(reference[0])
conf = translated.GetConformer()
for atom in range(translated.GetNumAtoms()):
    point = conf.GetAtomPosition(atom)
    conf.SetAtomPosition(atom, (point.x + 10, point.y, point.z))
check('RMSD negative control retains 10 A translation', close(rmsd(translated, reference[0]), 10),
      'fixed-frame symmetry-aware CalcRMS; deliberately translated reference')
budgets = rows('analysis/search-budget-comparison.csv')
check('three budget levels', [int(row['exhaustiveness']) for row in budgets] == [8, 16, 32],
      'analysis/search-budget-comparison.csv')
actual = {}
for effort, row in zip([8, 16, 32], budgets):
    name = f'e{effort}'
    receipt = read(f'runs/{name}/run.json')
    argv = receipt['argv']
    options = dict(zip(argv[1::2], argv[2::2]))
    check(name + ': completed real process', receipt['mode'] == 'real' and receipt['status'] == 'completed'
          and receipt['exitCode'] == 0 and receipt['processId'] > 0, f'runs/{name}/run.json')
    check(name + ': plan predates process', plan['createdBeforeExecution'] <= receipt['startedAt'] < receipt['endedAt'],
          f'runs/{name}/run.json')
    check(name + ': signed seed and fixed settings', options['--seed'] == '-1622165383'
          and options['--exhaustiveness'] == str(effort) and options['--cpu'] == '4'
          and options['--scoring'] == 'vina' and options['--spacing'] == '.375'
          and options['--num_modes'] == '9' and options['--energy_range'] == '3'
          and options['--min_rmsd'] == '1', f'runs/{name}/run.json')
    check(name + ': source bindings', receipt['inputs'] == plan['officialInputs']
          and options['--receptor'] == plan['solverInputBinding']['preparedReceptor']
          and options['--ligand'] == plan['solverInputBinding']['preparedLigand']
          and options['--config'] == plan['solverInputBinding']['box'], f'runs/{name}/run.json')
    check(name + ': executable hash', sha(Path(argv[0])) == plan['vinaDigest'], 'evidence/run-plan.json')
    log = (CASE / f'runs/{name}/vina.log').read_text()
    check(name + ': version and completion in log', 'AutoDock Vina v1.2.7' in log and
          'mode |' in log and '*' * 51 in log, f'runs/{name}/vina.log')
    check(name + ': output and log hashes', sha(CASE / f'runs/{name}/poses.pdbqt') == receipt['outputDigest']
          and sha(CASE / f'runs/{name}/vina.log') == receipt['logDigest'], f'runs/{name}/run.json')
    scores = [float(x) for x in re.findall(r'REMARK VINA RESULT:\s+([-\d.]+)',
        (CASE / f'runs/{name}/poses.pdbqt').read_text())]
    actual[name] = native_poses(f'runs/{name}/poses.pdbqt')
    check(name + ': SDF export preserves native coordinates at stored precision',
          export_matches(actual[name], poses(f'runs/{name}/poses.sdf')), f'runs/{name}/poses.sdf')
    check(name + ': native output and restored poses', len(scores) == len(actual[name]) == int(row['poseCount'])
          and all(m is not None and Chem.MolToSmiles(m) == identity for m in actual[name]), f'runs/{name}/poses.sdf')
    check(name + ': score and runtime table', close(scores[0], row['top1Score_kcal_per_mol'])
          and close(row['scoreDifference_kcal_per_mol'], scores[0] - reference_scores[0])
          and close(receipt['durationSeconds'], row['durationSeconds']), 'analysis/search-budget-comparison.csv')
    check(name + ': fixed-frame RMSD recomputation', close(rmsd(actual[name][0], crystal), row['top1CrystalRMSD_A'])
          and close(rmsd(actual[name][0], reference[0]), row['top1OfficialPoseRMSD_A']), 'analysis/search-budget-comparison.csv')

fixed = read('runs/fixed-score/run.json')
fixed_log = (CASE / 'runs/fixed-score/vina.log').read_text()
fixed_score = float(re.search(r'Estimated Free Energy of Binding\s*:\s*([-\d.]+)', fixed_log).group(1))
check('fixed pose rescore provenance', fixed['status'] == 'completed' and fixed['exitCode'] == 0
      and fixed['argv'][fixed['argv'].index('--ligand') + 1] == 'reference/official-top1.pdbqt'
      and sha(CASE / 'reference/official-top1.pdbqt') == fixed['fixedPoseInputDigest']
      and sha(CASE / 'runs/fixed-score/vina.log') == fixed['logDigest'], 'runs/fixed-score/run.json')
check('fixed pose score comparison', close(fixed_score, metrics['fixedReferencePoseCurrentScore_kcal_per_mol'])
      and close(fixed_score - reference_scores[0], metrics['fixedPoseScoreDifference_kcal_per_mol']), 'analysis/metrics.json')

matrix = np.array([[rmsd(a, b) for b in actual['e32']] for a in reference])
matrix_rows = rows('analysis/pose-rmsd-matrix.csv')
check('entire RMSD matrix recomputation', np.allclose(matrix,
      [[float(row[f'computedRank{j+1}']) for j in range(matrix.shape[1])] for row in matrix_rows], atol=1e-10),
      'analysis/pose-rmsd-matrix.csv')
ri, ci = linear_sum_assignment(matrix)
assignment = dict(zip(ri.tolist(), ci.tolist()))
comparison = rows('analysis/official-output-comparison.csv')
check('one-to-one minimum-total assignment', all((str(assignment[i] + 1) if i in assignment else '') ==
      row['matchedComputedRank'] for i, row in enumerate(comparison)), 'analysis/official-output-comparison.csv')
check('all pair residuals and threshold flags', all(close(matrix[i, assignment[i]], row['fixedFramePairRMSD_A'])
      and (row['poseMatchWithin2A'] == 'True') == (matrix[i, assignment[i]] <= 2)
      for i, row in enumerate(comparison) if i in assignment), 'analysis/official-output-comparison.csv')
check('matched pose energies and residuals', all(close(reference_scores[i], row['officialScore_kcal_per_mol'])
      and close(actual['e32'][assignment[i]].GetDoubleProp('VinaScore_kcal_per_mol'), row['computedScore_kcal_per_mol'])
      and close(float(row['computedScore_kcal_per_mol']) - reference_scores[i], row['scoreDifference_kcal_per_mol'])
      for i, row in enumerate(comparison) if i in assignment), 'analysis/official-output-comparison.csv')
check('matched mode count is honest', metrics['matchedOfficialModesWithin2A'] ==
      sum(row['poseMatchWithin2A'] == 'True' for row in comparison), 'analysis/metrics.json')

pb_rows = rows('analysis/posebusters-main.csv')
pb_keys = [key for key in pb_rows[0] if key not in ['file', 'molecule', 'position']]
pb_failures = [key for key in pb_keys if pb_rows[0][key].lower() != 'true']
check('PoseBusters rows and failures', len(pb_rows) == len(actual['e32']) and
      len(pb_keys) == metrics['top1PoseBustersChecks'] and pb_failures == metrics['top1PoseBustersFailures'],
      'analysis/posebusters-main.csv')
main = metrics['mainRun']
criteria = {'scoreDifference': abs(main['scoreDifference_kcal_per_mol']) <= .5,
    'officialPoseRMSD': main['top1OfficialPoseRMSD_A'] <= 2,
    'crystalRMSD': main['top1CrystalRMSD_A'] <= 2, 'posePlausibility': not pb_failures}
check('predeclared criterion aggregation', metrics['primaryCriteria'] == criteria, 'analysis/metrics.json')

pack = CASE / 'sources/domain-pack'
manifest = json.loads((pack / 'manifest.json').read_text(encoding='utf-8'))
check('domain resources snapshot hashes', all(sha(pack / item['path']) == item['digest']
      for item in manifest['resources']), 'sources/domain-pack/manifest.json')
schema = json.loads((pack / 'problem.schema.json').read_text(encoding='utf-8'))
validator = Draft202012Validator(schema)
task = read('task.json')
check('current task validates', not list(validator.iter_errors(task)), 'task.json')
for seed in [0, -2147483649, 2147483648]:
    invalid = copy.deepcopy(task)
    invalid['search']['seeds'] = [seed]
    check('seed rejected: ' + str(seed), bool(list(validator.iter_errors(invalid))), 'sources/domain-pack/problem.schema.json')
invalid = copy.deepcopy(task)
invalid['preparation']['stateSelectionMode'] = 'enumerateByPH'
check('inconsistent pH rejected', bool(list(validator.iter_errors(invalid))), 'sources/domain-pack/problem.schema.json')
for example in ['task.json', 'virtual-screening.task.json']:
    data = json.loads((pack / 'examples' / example).read_text(encoding='utf-8'))
    check('existing example valid: ' + example, not list(validator.iter_errors(data)), 'sources/domain-pack/examples/' + example)

for ledger, expected in [('parameter_ledger.csv', 21), ('conclusion_ledger.csv', 8), ('figure_ledger.csv', 2)]:
    check('ledger coverage: ' + ledger, len(rows('ledgers/' + ledger)) == expected, 'ledgers/' + ledger)
for row in rows('ledgers/conclusion_ledger.csv'):
    check('conclusion evidence exists: ' + row['id'], all((CASE / path.strip().split(': ')[0]).exists()
          for path in row['evidence'].split(';')), 'ledgers/conclusion_ledger.csv')
inspection = read('evidence/figure-inspection.json')
check('figures inspected against actual data', all(item['visuallyInspected'] and
      item['digest'] == sha(CASE / item['path']) for item in inspection['figures']), 'evidence/figure-inspection.json')
report = (CASE / 'README.md').read_text(encoding='utf-8')
links = re.findall(r'\]\(([^)]+)\)', report)
check('report local links resolve', all((CASE / link).exists() or link == 'evidence/case-validation.json'
      for link in links if not link.startswith(('http:', 'https:'))), 'README.md')
check('limitations disclosed', metrics['reproductionLevel'] == 'method_reproduction' and
      '起始坐标' in report and 'CPU' in report and '版本' in report, 'README.md')
if (CASE / 'evidence/resource-amendment.json').exists():
    amendment = read('evidence/resource-amendment.json')
    failed = read(amendment['failedAttempt'])
    retried = read('runs/e32/run.json')
    check('timeout failure preserved before identical retry', failed['status'] == 'timeout'
          and failed['argv'] == retried['argv'] and failed['inputs'] == retried['inputs']
          and amendment['recordedBeforeRetry'] < retried['startedAt']
          and retried['effectiveTimeoutSeconds'] == 5400
          and hashlib.sha256((CASE / amendment['failedAttempt']).read_bytes()).hexdigest() == amendment['failedReceiptSHA256'],
          'evidence/resource-amendment.json')
    check('timeout disclosed in report', '首次e32' in report and '超时' in report, 'README.md')
    check('all five execution attempts retained', len(list((CASE / 'runs').rglob('run.json'))) == 5,
          'runs: four original planned operations plus one documented retry')
stage_record = read('evidence/workflow-stage-record.json')
check('all nine workflow stages have input and output evidence', len(stage_record['stages']) == 9
      and all((CASE / path).exists() or path == 'artifact-index.json'
          for stage in stage_record['stages'] for path in stage['inputs'] + stage['outputs']),
      'evidence/workflow-stage-record.json')
check('qualification limitation retained', stage_record['qualificationException']['actual'] ==
      'official crystallographic initial coordinates', 'evidence/workflow-stage-record.json')

excluded = {'evidence/case-validation.json', 'artifact-index.json'}
artifacts = [{'path': p.relative_to(CASE).as_posix(), 'bytes': p.stat().st_size, 'digest': sha(p)}
    for p in sorted(CASE.rglob('*')) if p.is_file() and '__pycache__' not in p.parts
    and p.relative_to(CASE).as_posix() not in excluded]
(CASE / 'artifact-index.json').write_text(json.dumps({'artifacts': artifacts,
    'excludedSelfReferences': sorted(excluded)}, indent=2) + '\n', encoding='utf-8')
result = {'generatedAt': datetime.now(timezone.utc).isoformat(), 'auditPassed': all(c['passed'] for c in checks),
    'passed': sum(c['passed'] for c in checks), 'total': len(checks),
    'scientificPrimaryCriteria': criteria, 'officialModesMatched': metrics['matchedOfficialModesWithin2A'],
    'checks': checks, 'artifactIndexDigest': sha(CASE / 'artifact-index.json'),
    'note': 'Audit success means evidence is internally consistent; scientific failures remain reported.'}
(CASE / 'evidence/case-validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({k: v for k, v in result.items() if k != 'checks'}, ensure_ascii=True), flush=True)
for item in checks:
    if not item['passed']:
        print('FAILED: ' + item['check'])
sys.exit(0 if result['auditPassed'] else 1)
