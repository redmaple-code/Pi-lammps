"""Compare Windows encoding modes for the failing MCP contract suite only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ap = argparse.ArgumentParser()
ap.add_argument('zip', type=Path)
ap.add_argument('--output', type=Path, required=True)
args = ap.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
rows = []
with tempfile.TemporaryDirectory(prefix='mdhandoff-encoding-') as temp:
    root = Path(temp).resolve()
    with zipfile.ZipFile(args.zip) as z:
        for info in z.infolist():
            if not (root/info.filename).resolve().is_relative_to(root):
                raise ValueError('unsafe zip path')
        z.extractall(root)
    package = next(p.parent for p in root.glob('*/PACKAGE-MANIFEST.json'))
    base = {k:v for k,v in os.environ.items()
            if not k.startswith(('MDSIM_','MDAGENT_','MDPROJ_','REVIEW_','UKB_'))
            and k not in ('PYTHONIOENCODING','PYTHONUTF8','PYTHONPATH')}
    base.update(MDSIM_DATA_ROOT=str(root/'data'), MDPROJ_ROOT=str(root/'projects'), PYTHONDONTWRITEBYTECODE='1')
    for name, settings in [('native-locale', {}), ('stdio-utf8-only', {'PYTHONIOENCODING':'utf-8'}),
                           ('full-utf8', {'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'})]:
        command = [sys.executable,'-m','unittest','discover','-s','tests','-t','tests','-p','test_md_mcp_contract.py']
        env = {**base, **settings}
        encoding = subprocess.check_output([sys.executable,'-c','import locale;print(locale.getencoding())'],env=env).decode('ascii').strip()
        proc = subprocess.run(command,cwd=package/'src',env=env,capture_output=True,timeout=120)
        # Console bytes are not the protocol under test; keep the original raw log.
        (args.output/(name+'.log')).write_bytes(proc.stdout+proc.stderr)
        rows.append({'mode':name,'settings':settings,'locale_encoding':encoding,'returncode':proc.returncode,
                     'command':command,'cwd':'<disposable-package>/src'})
(args.output/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(rows,ensure_ascii=False,indent=2))
