"""Rerun the supplied runner in a disposable copy, without inheriting service roots.

Only reports/logs are exported. Never copies stores or credentials to evidence.
Usage: python recheck_package.py ZIP --output DIRECTORY
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('zip', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('MDSIM_', 'MDAGENT_', 'MDPROJ_', 'REVIEW_', 'UKB_'))
           and k not in ('PYTHONPATH',)}
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    report = {'zip_sha256': hashlib.sha256(args.zip.read_bytes()).hexdigest(),
              'started_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
              'method': 'original runner in disposable copy; service environment removed',
              'python': sys.version, 'steps': []}
    with tempfile.TemporaryDirectory(prefix='mdhandoff-review-') as temp:
        root = Path(temp).resolve()
        with zipfile.ZipFile(args.zip) as z:
            for info in z.infolist():
                if not (root / info.filename).resolve().is_relative_to(root):
                    raise ValueError('unsafe zip path')
            z.extractall(root)
        package = next(p.parent for p in root.glob('*/PACKAGE-MANIFEST.json'))
        env['MDSIM_DATA_ROOT'] = str(root / 'runtime-data')
        env['MDPROJ_ROOT'] = str(root / 'projects')
        commands = [('manifest-before', [sys.executable, 'tools/verify_package.py', '--strict-extra']),
                    ('original-runner', [sys.executable, 'tools/run_verification.py'])]
        for name, command in commands:
            proc = subprocess.run(command, cwd=package, env=env, capture_output=True,
                                  text=True, encoding='utf-8', errors='replace', timeout=1500)
            (out / (name + '.log')).write_text(proc.stdout + proc.stderr, encoding='utf-8')
            report['steps'].append({'name': name, 'returncode': proc.returncode})
            print(name, proc.returncode, flush=True)
        rerun = out / 'rerun'
        rerun.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package / 'verification/RESULTS.json', rerun / 'RESULTS.json')
        shutil.copytree(package / 'verification/logs', rerun / 'logs', dirs_exist_ok=True)
        # Do not read or export secrets. Their existence alone demonstrates cleanup behavior.
        report['leftover_credential_paths'] = [str(p.relative_to(package)).replace('\\', '/')
            for p in package.rglob('*') if p.is_file()
            and (p.name in ('admin.json', 'ukb_admin.json', 'system_auto.token'))]
        report['finished_at'] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        (out / 'recheck.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if all(s['returncode'] == 0 for s in report['steps']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
