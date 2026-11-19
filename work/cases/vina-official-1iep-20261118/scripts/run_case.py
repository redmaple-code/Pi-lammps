"""Reproduce the fixed public Vina example and its search-budget comparison."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from jsonschema import Draft202012Validator

CASE = Path(__file__).resolve().parents[1]
WORKSPACE = CASE.parents[2]
PACK = WORKSPACE / 'docs/domain-workflows/small-molecule-docking'
ENV = Path(os.environ['LOCALAPPDATA']) / 'RedMaple/simulation-envs/docking-v1'
VINA = ENV / 'bin/vina_1.2.7_win.exe'
SEED = -1622165383


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def table(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


sources = json.loads((CASE / 'sources/source-lock.json').read_text(encoding='utf-8'))
for item in sources['files']:
    if sha(CASE / item['localPath']) != 'sha256:' + item['sha256']:
        raise RuntimeError('Official source digest changed.')
if not (CASE / 'sources/domain-pack').exists():
    shutil.copytree(PACK, CASE / 'sources/domain-pack')
PACK = CASE / 'sources/domain-pack'
version = subprocess.check_output([str(VINA), '--version'], text=True).strip()
if '1.2.7' not in version:
    raise RuntimeError('Unexpected Vina version.')
task = json.loads((PACK / 'examples/task.json').read_text(encoding='utf-8'))
task['taskId'] = 'vina-official-1iep-20261118'
task['parameterStatus'] = 'locked'
task['target']['structure'] = {'id': 'official-prehydrogenated-receptor',
    'locator': 'reference/receptorH.pdb', 'status': 'acquired', 'digest': sha(CASE / 'reference/receptorH.pdb')}
task['target']['provenance'] = sources['repository'] + '/tree/' + sources['commit']
task['ligands']['input'] = {'id': 'official-ligand-identity', 'locator': 'reference/crystal-ligand.sdf',
    'status': 'acquired', 'digest': sha(CASE / 'reference/crystal-ligand.sdf')}
task['preparation']['stateSelectionMode'] = 'fixedProvidedState'
task['preparation']['pHRange'] = None
task['preparation']['maxStatesPerParent'] = 1
task['preparation']['stateEnumerationMethod'] = 'Use official prepared PDBQT unchanged; one provided charge state.'
task['search'].update({'seeds': [SEED], 'numModes': 9, 'cpuPerRun': 4})
task['qualification']['referenceLigand'] = {'id': 'crystal-reference', 'locator': 'reference/crystal-ligand.sdf',
    'status': 'acquired', 'digest': sha(CASE / 'reference/crystal-ligand.sdf')}
task['resources'].update({'maxRuns': 4, 'wallTimePerRun': {'value': 1800, 'unit': 's'}, 'maxConcurrentRuns': 2})
schema = json.loads((PACK / 'problem.schema.json').read_text(encoding='utf-8'))
Draft202012Validator.check_schema(schema)
Draft202012Validator(schema).validate(task)
dump(CASE / 'task.json', task)

input_paths = ['inputs/official/1iep_receptor.pdbqt', 'inputs/official/1iep_ligand.pdbqt',
               'inputs/official/1iep_receptor.box.txt']
inputs = [{'path': relative, 'digest': sha(CASE / relative)} for relative in input_paths]
plan = {'createdBeforeExecution': datetime.now(timezone.utc).isoformat(), 'caseId': task['taskId'],
    'mode': 'realReferenceReproduction', 'sourceCommit': sources['commit'],
    'officialInputs': inputs, 'vinaVersion': version, 'vinaDigest': sha(VINA),
    'mainRun': 'e32', 'searchBudgetStudy': [8, 16, 32], 'seed': SEED,
    'cpuPerRun': 4, 'maxConcurrentRuns': 2, 'timeoutSeconds': 1800,
    'solverInputBinding': {'preparedReceptor': input_paths[0], 'preparedLigand': input_paths[1], 'box': input_paths[2]},
    'expected': {'officialTop1Score_kcal_per_mol': -13.234,
        'top1AbsoluteScoreTolerance_kcal_per_mol': .5, 'top1OfficialPoseRMSDLimit_A': 2,
        'top1CrystalRMSDLimit_A': 2, 'officialOutputModes': 4},
    'referenceVersionLimitations': ['Reference output producer version and hardware were not recorded.',
        'The documentation uses CPU auto-detection; the current run fixes four threads.'],
    'reproductionLevel': 'method_reproduction',
    'notApplicable': {'physicalTimeStep': 'Stochastic pose search has no physical time integration.',
                      'PDEMeshConvergence': 'No PDE mesh is solved; grid spacing is a scoring/search configuration.'}}
plan_path = CASE / 'evidence/run-plan.json'
if plan_path.exists():
    existing = json.loads(plan_path.read_text(encoding='utf-8'))
    for key in ['officialInputs', 'seed', 'expected', 'vinaDigest', 'searchBudgetStudy']:
        if existing[key] != plan[key]:
            raise RuntimeError('Locked run plan differs; create a new revision.')
else:
    dump(plan_path, plan)

parameter_rows = []
for name, value, unit, provenance, implementation in [
    ('receptor', input_paths[0], 'file', 'official solution', 'unaltered bytes'),
    ('ligand', input_paths[1], 'file', 'official solution', 'unaltered bytes'),
    ('crystal reference', 'reference/crystal-ligand.sdf', 'file', 'official data', 'unaltered bytes'),
    ('reference output', 'reference/official-vina-output.pdbqt', 'file', 'official solution', 'unaltered bytes'),
    ('box center', '15.190,53.903,16.917', 'angstrom', 'official box.txt', 'same'),
    ('box size', '20,20,20', 'angstrom', 'official box.txt', 'same'),
    ('scoring', 'vina', 'name', 'tutorial Vina branch', 'same'),
    ('seed', SEED, 'integer', 'tutorial section5.b', 'same signed seed'),
    ('exhaustiveness', 32, 'integer', 'official command', 'main e32; sensitivity8/16'),
    ('num_modes', 9, 'integer', 'Vina1.2.7 default', 'explicit'),
    ('energy_range', 3, 'kcal/mol', 'Vina default/output cutoff', 'explicit'),
    ('min_rmsd', 1, 'angstrom', 'Vina default', 'explicit'),
    ('spacing', .375, 'angstrom', 'tutorial log', 'explicit'),
    ('cpu', 'auto; effective count unknown', 'threads', 'official log CPU0', '4; disclosed difference'),
    ('solver', 'reference producer version unknown', 'version', 'reference file lacks version', 'Vina1.2.7'),
    ('ligand state', 'provided +1 state', 'formalCharge', 'official structure', 'fixed; no pH enumeration'),
    ('scope', 'rigid receptor/flexible ligand, known site', 'model', 'official tutorial', 'same'),
    ('timeout', 1800, 's', 'implementation resource bound', 'new execution setting'),
    ('score tolerance', .5, 'kcal/mol', 'predeclared engineering target', 'same for all comparisons'),
    ('reference RMSD limit', 2, 'angstrom', 'predeclared recovery target', 'no ligand alignment'),
    ('physical time step', 'not applicable', 'none', 'stochastic docking', 'not introduced')]:
    parameter_rows.append({'parameter': name, 'referenceValue': value, 'unit': unit,
        'source': provenance, 'implementationAndDifference': implementation})
table(CASE / 'ledgers/parameter_ledger.csv', parameter_rows)

reference_output = (CASE / 'reference/official-vina-output.pdbqt').read_text()
first_model = reference_output.split('ENDMDL', 1)[0]
first_model = '\n'.join(line for line in first_model.splitlines() if not line.startswith('MODEL')) + '\n'
fixed_pose = CASE / 'reference/official-top1.pdbqt'
fixed_pose.write_text(first_model, encoding='utf-8')


def execute(run_id, extra, ligand_path='inputs/official/1iep_ligand.pdbqt', timeout=1800):
    amendment = CASE / 'evidence/resource-amendment.json'
    if amendment.exists():
        timeout = json.loads(amendment.read_text(encoding='utf-8'))['effectiveTimeoutSecondsByRun'].get(run_id, timeout)
    directory = CASE / 'runs' / run_id
    directory.mkdir(exist_ok=True)
    receipt_file = directory / 'run.json'
    output = directory / 'poses.pdbqt'
    if receipt_file.exists():
        old = json.loads(receipt_file.read_text(encoding='utf-8'))
        expected_command = [str(VINA), '--receptor', input_paths[0], '--ligand', ligand_path,
            '--config', input_paths[2], '--scoring', 'vina', '--cpu', '4', '--spacing', '.375'] + extra
        if run_id != 'fixed-score':
            expected_command += ['--out', f'runs/{run_id}/poses.pdbqt']
        reusable = old['status'] == 'completed' and old['exitCode'] == 0 and old['argv'] == expected_command
        reusable = reusable and old['inputs'] == inputs and old['logDigest'] == sha(directory / 'vina.log')
        reusable = reusable and (old.get('fixedPoseInputDigest') == sha(fixed_pose) if run_id == 'fixed-score'
            else old.get('outputDigest') == sha(output))
        if reusable:
            return old
        raise RuntimeError(f'Reconcile existing run first: {run_id}')
    command = [str(VINA), '--receptor', input_paths[0], '--ligand', ligand_path,
               '--config', input_paths[2], '--scoring', 'vina', '--cpu', '4', '--spacing', '.375'] + extra
    if run_id != 'fixed-score':
        command += ['--out', f'runs/{run_id}/poses.pdbqt']
    record = {'runId': run_id, 'operationId': 'official-1iep-v001-' + run_id,
        'argv': command, 'workdir': str(CASE), 'mode': 'real', 'inputs': inputs,
        'startedAt': datetime.now(timezone.utc).isoformat(), 'status': 'intentRecorded'}
    record['effectiveTimeoutSeconds'] = timeout
    dump(receipt_file, record)
    timer = time.monotonic()
    with (directory / 'vina.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=CASE, stdout=log, stderr=subprocess.STDOUT)
        record['processId'] = process.pid
        record['status'] = 'running'
        dump(receipt_file, record)
        try:
            process.wait(timeout=timeout)
            record['exitCode'] = process.returncode
            record['status'] = 'completed' if process.returncode == 0 else 'failed'
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            record['status'] = 'timeout'
    record['durationSeconds'] = time.monotonic() - timer
    record['endedAt'] = datetime.now(timezone.utc).isoformat()
    if run_id != 'fixed-score' and output.exists():
        record['outputDigest'] = sha(output)
    if run_id == 'fixed-score':
        record['fixedPoseInputDigest'] = sha(fixed_pose)
    record['logDigest'] = sha(directory / 'vina.log')
    dump(receipt_file, record)
    print(json.dumps({'run': run_id, 'status': record['status'], 'seconds': record['durationSeconds']}), flush=True)
    return record


fixed = execute('fixed-score', ['--score_only'], 'reference/official-top1.pdbqt', 180)
if fixed['status'] != 'completed':
    raise RuntimeError('Reference pose scoring did not complete.')
with ThreadPoolExecutor(max_workers=2) as pool:
    futures = [pool.submit(execute, f'e{effort}', ['--seed', str(SEED), '--exhaustiveness', str(effort),
        '--num_modes', '9', '--energy_range', '3', '--min_rmsd', '1']) for effort in [32, 8, 16]]
    records = [future.result() for future in as_completed(futures)]
dump(CASE / 'evidence/run-summary.json', [fixed] + records)
if not all(record['status'] == 'completed' for record in records):
    raise RuntimeError('Some searches did not complete; evidence is preserved.')
