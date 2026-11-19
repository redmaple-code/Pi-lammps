"""Supplement existing results with PoseBusters' unchanged redocking checks."""

from datetime import datetime, timezone
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys
import time

from posebusters import PoseBusters

CASE = Path(__file__).resolve().parents[1]
OUT = CASE / 'validation-review'


def plain(value):
    return value.item() if hasattr(value, 'item') else value


def summarize(frame):
    columns = frame.select_dtypes(include=['bool', 'boolean']).columns.tolist()
    if len(columns) < 20:
        raise RuntimeError(f'Unexpectedly few check columns: {columns}')
    rows = []
    for index, row in frame.iterrows():
        failed = [name for name in columns if not bool(row[name])]
        rows.append({'index': [plain(v) for v in index] if isinstance(index, tuple) else str(index),
                     'checkCount': len(columns), 'failedChecks': failed, 'allChecksPassed': len(failed) == 0})
    return columns, rows


timer = time.monotonic()
buster = PoseBusters(config='redock', max_workers=0)
(OUT / 'posebusters-config.json').write_text(json.dumps(buster.config, indent=2) + '\n', encoding='utf-8')
files = [CASE / 'runs' / f'seed-{seed}' / 'poses.sdf' for seed in [104729, 130363, 155921]]
reference = CASE / 'inputs/prepared/reference-heavy.sdf'
protein = CASE / 'inputs/prepared/receptor-prepared.pdb'
frame = buster.bust(mol_pred=files, mol_true=reference, mol_cond=protein, full_report=False)
frame.to_csv(OUT / 'posebusters-all-poses.csv')
columns, results = summarize(frame)
print(json.dumps({'posesChecked': len(results), 'checksPerPose': len(columns),
                  'allChecksPassed': sum(row['allChecksPassed'] for row in results)}), flush=True)
reference_frame = PoseBusters(config='redock', max_workers=0).bust(
    mol_pred=reference, mol_true=reference, mol_cond=protein, full_report=False)
reference_frame.to_csv(OUT / 'posebusters-reference.csv')
_, reference_results = summarize(reference_frame)
dist = metadata.distribution('posebusters')
license_files = []
for relative in dist.files or []:
    source = Path(dist.locate_file(relative))
    if '.dist-info/' in str(relative).lower() and 'license' in str(relative).lower() and source.is_file():
        destination = OUT / 'licenses' / Path(*Path(relative).parts[1:])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        license_files.append(destination.relative_to(OUT).as_posix())
result = {'tool': 'PoseBusters', 'version': metadata.version('posebusters'),
          'checkedAt': datetime.now(timezone.utc).isoformat(),
          'configuration': 'Unmodified redock defaults; saved in posebusters-config.json',
          'durationSeconds': time.monotonic() - timer,
          'checkColumns': columns, 'poses': results, 'reference': reference_results,
          'licenseExpression': dist.metadata.get('License-Expression'),
          'licenseField': dist.metadata.get('License'), 'licenseFiles': license_files,
          'installedLicenseHeader': (OUT / license_files[0]).read_text(encoding='utf-8').splitlines()[0] if license_files else None,
          'limitations': ['Checks use the deployed single-receptor model.',
                          'Cofactor/water absence can yield vacuous passes for those categories.',
                          'These tests evaluate pose plausibility under their configured tolerances.']}
(OUT / 'posebusters-summary.json').write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
freeze = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], text=True)
(OUT / 'requirements.lock.txt').write_text(freeze, encoding='utf-8')
print(json.dumps(result, indent=2, default=str), flush=True)
