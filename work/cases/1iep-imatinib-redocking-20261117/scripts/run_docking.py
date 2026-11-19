"""Run three predeclared Vina searches, with per-run evidence and bounded resources."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

CASE = Path(__file__).resolve().parents[1]
VINA = Path(os.environ['LOCALAPPDATA']) / 'RedMaple/simulation-envs/docking-v1/bin/vina_1.2.7_win.exe'
SEEDS = [104729, 130363, 155921]


def sha(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


configuration = '\n'.join([
    'center_x = 15.190', 'center_y = 53.903', 'center_z = 16.917',
    'size_x = 20', 'size_y = 20', 'size_z = 20', 'scoring = vina',
    'exhaustiveness = 32', 'num_modes = 20', 'energy_range = 3', 'cpu = 4']) + '\n'
config_path = CASE / 'inputs/prepared/search.cfg'
config_path.write_text(configuration, encoding='utf-8')
input_paths = [CASE / 'inputs/prepared/receptor.pdbqt', CASE / 'inputs/prepared/ligand.pdbqt', config_path]
inputs = [{'path': path.relative_to(CASE).as_posix(), 'digest': sha(path)} for path in input_paths]
plan_path = CASE / 'evidence/run-plan.json'
plan = {'mode': 'real', 'seeds': SEEDS, 'cpuPerRun': 4, 'maxConcurrentRuns': 2,
        'timeoutPerRunSeconds': 900, 'inputs': inputs, 'vinaDigest': sha(VINA),
        'referenceMetric': 'Unaligned symmetry-aware heavy-atom RMSD in fixed receptor coordinates',
        'RMSDLimit_A': 2.0, 'top1SuccessTarget': 'at least2/3',
        'declaredBeforeExecution': datetime.now(timezone.utc).isoformat()}
if not plan_path.exists():
    plan_path.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
else:
    old = json.loads(plan_path.read_text(encoding='utf-8'))
    if old['inputs'] != inputs or old['seeds'] != SEEDS or old['vinaDigest'] != sha(VINA):
        raise RuntimeError('Existing run plan differs. Create a new case revision.')


def execute(seed):
    directory = CASE / 'runs' / f'seed-{seed}'
    directory.mkdir(exist_ok=True)
    receipt_path = directory / 'run.json'
    output = directory / 'poses.pdbqt'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        if receipt['status'] == 'completed' and receipt.get('outputDigest') == sha(output):
            return receipt
        raise RuntimeError(f'Existing failed or uncertain run requires explicit reconciliation: {seed}')
    if output.exists():
        raise RuntimeError(f'Output exists without a run receipt: {seed}')
    command = [str(VINA), '--receptor', 'inputs/prepared/receptor.pdbqt',
               '--ligand', 'inputs/prepared/ligand.pdbqt', '--config', 'inputs/prepared/search.cfg',
               '--seed', str(seed), '--out', f'runs/seed-{seed}/poses.pdbqt']
    receipt = {'runId': f'1iep-seed-{seed}', 'operationId': f'1iep-v001-seed-{seed}',
               'mode': 'real', 'seed': seed, 'argv': command, 'workdir': str(CASE),
               'inputs': inputs, 'status': 'intentRecorded',
               'startedAt': datetime.now(timezone.utc).isoformat(),
               'stdoutStderr': f'runs/seed-{seed}/vina.log'}
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    timer = time.monotonic()
    try:
        with (directory / 'vina.log').open('w', encoding='utf-8') as log:
            process = subprocess.run(command, cwd=CASE, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        receipt['exitCode'] = process.returncode
        receipt['status'] = 'completed' if process.returncode == 0 and output.is_file() and output.stat().st_size else 'failed'
        if output.is_file():
            receipt['outputDigest'] = sha(output)
    except subprocess.TimeoutExpired:
        receipt['status'] = 'timeout'
    except OSError as error:
        receipt['status'] = 'failed'
        receipt['error'] = str(error)
    receipt['durationSeconds'] = time.monotonic() - timer
    receipt['endedAt'] = datetime.now(timezone.utc).isoformat()
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'seed': seed, 'status': receipt['status'],
                      'durationSeconds': receipt['durationSeconds']}), flush=True)
    return receipt


with ThreadPoolExecutor(max_workers=2) as pool:
    receipts = [future.result() for future in as_completed([pool.submit(execute, seed) for seed in SEEDS])]
(CASE / 'evidence/run-summary.json').write_text(json.dumps(receipts, indent=2) + '\n', encoding='utf-8')
if not all(row['status'] == 'completed' for row in receipts):
    raise RuntimeError('At least one docking run did not complete. See the preserved evidence.')
