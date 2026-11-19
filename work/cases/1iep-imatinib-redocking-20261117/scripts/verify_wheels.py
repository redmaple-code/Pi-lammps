"""Compare pip's downloaded wheel digests against PyPI's version metadata."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import requests

CASE = Path(__file__).resolve().parents[1]
report = json.loads((CASE / 'evidence/pip-install-report.json').read_text(encoding='utf-8'))


def verify(item):
    name = item['metadata']['name']
    version = item['metadata']['version']
    downloaded = item['download_info']['archive_info']['hashes']['sha256']
    session = requests.Session()
    session.trust_env = False
    url = f'https://pypi.org/pypi/{name}/{version}/json'
    response = session.get(url, timeout=30)
    response.raise_for_status()
    metadata = response.json()
    matches = [file for file in metadata['urls'] if file['digests']['sha256'] == downloaded]
    return {'name': name, 'version': version, 'officialMetadata': url,
            'downloadUrl': item['download_info']['url'], 'sha256': downloaded,
            'officialHashMatched': bool(matches),
            'matchingFiles': [file['filename'] for file in matches]}


with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(verify, report['install']))
(CASE / 'evidence/wheel-integrity.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
if not all(row['officialHashMatched'] for row in results):
    raise RuntimeError('Wheel hash absent from official PyPI metadata.')
print(json.dumps({'verifiedWheels': len(results), 'allMatchOfficialPyPI': True}), flush=True)
