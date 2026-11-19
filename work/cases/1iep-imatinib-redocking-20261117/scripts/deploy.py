"""Deploy the open-source docking tools and fetch fixed public tutorial inputs."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

CASE = Path(__file__).resolve().parents[1]
ENV = Path(os.environ['LOCALAPPDATA']) / 'RedMaple/simulation-envs/docking-v1'
PYTHON = ENV / 'Scripts/python.exe'
BIN = ENV / 'bin'
BIN.mkdir(exist_ok=True)
SESSION = requests.Session()
SESSION.trust_env = False
DOWNLOADS = []
CACHED_DOWNLOADS = json.loads((CASE / 'evidence/downloads.json').read_text(encoding='utf-8')) if (CASE / 'evidence/downloads.json').exists() else []


def fetch(url, path):
    cached = next((row for row in CACHED_DOWNLOADS if row['url'] == url and row['path'] == str(path)), None)
    if cached and path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == cached['sha256']:
        DOWNLOADS.append(cached)
        print(json.dumps({'verifiedCachedDownload': path.name}), flush=True)
        return cached
    response = SESSION.get(url, timeout=90)
    response.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    resolved = urlsplit(response.url)
    receipt = {'url': url, 'resolvedUrl': urlunsplit((resolved.scheme, resolved.netloc, resolved.path, '', '')),
               'resolvedQueryOmitted': bool(resolved.query), 'path': str(path),
               'sha256': hashlib.sha256(response.content).hexdigest(),
               'bytes': len(response.content),
               'downloadedAt': datetime.now(timezone.utc).isoformat()}
    DOWNLOADS.append(receipt)
    print(json.dumps({'downloaded': path.name, 'bytes': len(response.content)}), flush=True)
    return receipt


commit = 'bb64e5e26b520569a86360d69248069b5e665b3f'
for name in ['1iep_ligand.sdf', '1iep_receptorH.pdb']:
    fetch(f'https://raw.githubusercontent.com/forlilab/AutoDock-Vina/{commit}/example/basic_docking/data/{name}',
          CASE / 'inputs/raw' / name)
fetch('https://github.com/forlilab/AutoDock-Vina/releases/download/v1.2.7/vina_1.2.7_win.exe',
      BIN / 'vina_1.2.7_win.exe')
fetch('https://raw.githubusercontent.com/forlilab/AutoDock-Vina/v1.2.7/LICENSE',
      CASE / 'licenses/AutoDock-Vina-1.2.7-LICENSE.txt')
fetch('https://raw.githubusercontent.com/forlilab/Meeko/v0.8.0/LICENSE',
      CASE / 'licenses/Meeko-0.8.0-LICENSE.txt')
(CASE / 'evidence/downloads.json').write_text(json.dumps(DOWNLOADS, indent=2) + '\n', encoding='utf-8')
clean_env = {k: v for k, v in os.environ.items() if k.lower() not in
             ['http_proxy', 'https_proxy', 'all_proxy', 'no_proxy']}
clean_env['NO_PROXY'] = '*'
command = [str(PYTHON), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
           '--retries', '1', '--timeout', '30', '--report', str(CASE / 'evidence/pip-install-report.json'),
           '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple', 'rdkit', 'meeko==0.8.0',
           'gemmi', 'numpy', 'scipy', 'matplotlib', 'jsonschema']
with (CASE / 'logs/pip-install.log').open('w', encoding='utf-8') as log:
    process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=clean_env)
if process.returncode:
    print((CASE / 'logs/pip-install.log').read_text(encoding='utf-8')[-6000:], flush=True)
    sys.exit(process.returncode)
freeze = subprocess.check_output([str(PYTHON), '-m', 'pip', 'freeze'], text=True)
(CASE / 'requirements.lock.txt').write_text(freeze, encoding='utf-8')
version = subprocess.check_output([str(BIN / 'vina_1.2.7_win.exe'), '--version'], text=True)
(CASE / 'logs/vina-version.txt').write_text(version, encoding='utf-8')
(CASE / 'evidence/deployment.json').write_text(json.dumps({
    'environment': str(ENV), 'python': str(PYTHON), 'vina': str(BIN / 'vina_1.2.7_win.exe'),
    'tutorialCommit': commit, 'vinaVersion': version.strip(),
    'pipExitCode': process.returncode, 'installCommand': command,
    'downloadReceipt': 'evidence/downloads.json', 'requirements': 'requirements.lock.txt',
    'scope': 'Dedicated local environment and this case directory.'}, indent=2) + '\n', encoding='utf-8')
print(freeze, flush=True)
print(version, flush=True)
