"""Capture public package/runtime provenance, excluding credentials and host identity."""

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

CASE = Path(__file__).resolve().parents[1]
VINA = Path(sys.executable).parents[1] / 'bin/vina_1.2.7_win.exe'
packages = []
for name in ['rdkit', 'meeko', 'gemmi', 'numpy', 'scipy', 'matplotlib', 'jsonschema', 'posebusters']:
    dist = metadata.distribution(name)
    packages.append({'name': name, 'version': dist.version,
        'declaredLicense': dist.metadata.get('License-Expression') or dist.metadata.get('License')})
receipt = {'capturedAt': datetime.now(timezone.utc).isoformat(), 'pythonVersion': platform.python_version(),
    'platform': platform.platform(), 'architecture': platform.machine(), 'logicalCpuCount': os.cpu_count(),
    'vinaVersion': subprocess.check_output([str(VINA), '--version'], text=True).strip(),
    'vinaSHA256': hashlib.sha256(VINA.read_bytes()).hexdigest(),
    'vinaReleaseAsset': 'https://github.com/forlilab/AutoDock-Vina/releases/download/v1.2.7/vina_1.2.7_win.exe',
    'packages': packages, 'scope': 'Existing dedicated local docking-v1 environment; no new solver installation needed.'}
(CASE / 'evidence/environment.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'pythonVersion': receipt['pythonVersion'], 'vinaVersion': receipt['vinaVersion'],
    'packages': [{'name': item['name'], 'version': item['version']} for item in packages]}, ensure_ascii=True), flush=True)
