"""Preserve a timed-out attempt and authorize a longer resource envelope only."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

CASE = Path(__file__).resolve().parents[1]
run = CASE / 'runs/e32'
old = json.loads((run / 'run.json').read_text(encoding='utf-8'))
if old['status'] != 'timeout':
    raise RuntimeError('Only an explicitly timed-out attempt can be recovered here.')
archive = CASE / 'runs/attempts/e32-timeout-01'
for path in [run, archive]:
    path.resolve().relative_to(CASE.resolve())
if archive.exists() or (CASE / 'evidence/resource-amendment.json').exists():
    raise RuntimeError('Recovery already exists; reconcile before another attempt.')
archive.parent.mkdir(exist_ok=True)
shutil.move(str(run), str(archive))
shutil.copyfile(CASE / 'evidence/run-summary.json', CASE / 'evidence/run-summary-attempt01.json')
amendment = {'recordedBeforeRetry': datetime.now(timezone.utc).isoformat(),
    'reason': 'Main search exceeded the initial 1800-second execution budget; preserve failure and repeat same solver inputs/settings.',
    'originalPlan': 'evidence/run-plan.json', 'failedAttempt': 'runs/attempts/e32-timeout-01/run.json',
    'failedReceiptSHA256': hashlib.sha256((archive / 'run.json').read_bytes()).hexdigest(),
    'effectiveTimeoutSecondsByRun': {'e32': 5400},
    'unchanged': ['official input bytes', 'signed seed', 'exhaustiveness=32', 'cpu=4', 'scoring',
                  'grid', 'output cutoffs', 'scientific acceptance criteria'],
    'execution': 'e8/e16/fixed-score completed outputs are hash-verified and reused; only e32 is repeated.'}
(CASE / 'evidence/resource-amendment.json').write_text(json.dumps(amendment, indent=2) + '\n', encoding='utf-8')
print('Preserved timeout; restarting only e32 with a 5400-second resource envelope.', flush=True)
subprocess.run([sys.executable, str(CASE / 'scripts/run_case.py')], cwd=CASE, check=True)
