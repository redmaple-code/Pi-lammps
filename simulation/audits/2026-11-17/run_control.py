"""Matched no-abrasive control, keeping the original seed, timing and thermostat."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
SIMULATION = ROOT.parents[1]
SOURCE = SIMULATION / 'work/jobs/lammps-f36c8580-d0a7-4e3b-9068-3d190415964d/work'
TARGET = ROOT / 'no-tool-control'
TARGET.mkdir(exist_ok=False)
for name in ['alloy.data', 'FeNiCrCoCu-heafixed.setfl', 'structure-report.json']:
    shutil.copyfile(SOURCE / name, TARGET / name)
original = (SOURCE / 'in.grinding').read_text(encoding='utf-8')
modified = re.sub(r'^fix tool .*\n', '', original, flags=re.M)
modified = re.sub(r'^unfix tool\n', '', modified, flags=re.M)
modified = re.sub(r'^variable (fx|fy|fz) equal f_tool\[[123]\]', r'variable \1 equal 0.0', modified, flags=re.M)
assert 'f_tool' not in modified and ' indent ' not in modified
modified = '# Audit matched control with zero abrasive force.\n' + modified
(TARGET / 'in.grinding').write_text(modified, encoding='utf-8')
argv = [r'E:\LAMMPS 64-bit 2Aug2023-MSMPI\bin\lmp.exe', '-in', 'in.grinding', '-log', 'log.lammps']
start = datetime.now(timezone.utc).isoformat()
clock = time.monotonic()
with (TARGET / 'solver.stdout.log').open('w', encoding='utf-8') as stdout, (TARGET / 'solver.stderr.log').open('w', encoding='utf-8') as stderr:
    result = subprocess.run(argv, cwd=TARGET, stdout=stdout, stderr=stderr, timeout=1800)
receipt = {'status': 'succeeded' if result.returncode == 0 else 'failed', 'exitCode': result.returncode,
           'startedAt': start, 'endedAt': datetime.now(timezone.utc).isoformat(),
           'wallSeconds': time.monotonic() - clock, 'argv': argv, 'directory': str(TARGET),
           'sourceInputSha256': hashlib.sha256(original.encode()).hexdigest(),
           'inputSha256': hashlib.sha256(modified.encode()).hexdigest(),
           'purpose': 'Compare equilibrium thermal/relaxation displacement against loaded case, with same initial thermal seed',
           'changedPhysics': 'Analytical abrasive forces removed, matching schedules, groups and thermostat retained'}
(TARGET / 'receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
print(json.dumps(receipt))
raise SystemExit(result.returncode)
