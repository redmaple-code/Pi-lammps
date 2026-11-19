"""Build standalone archives from the exact 0.1.1 modular source baseline."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT.parent
PACKAGES=PROJECT/'mdagent-modules/packages/0.1.1'
COMPOSE=PROJECT/'mdagent-modules/tools/split_handover.py'
OUT=ROOT/'packages/0.2.0'
NAMES=('knowledge','evidence','gate','consistency')
OUT.mkdir(parents=True,exist_ok=False)

for name in NAMES:
    target=OUT/name
    proc=subprocess.run([sys.executable,str(COMPOSE),'compose','--packages',str(PACKAGES),'--out',str(target),name],
                        capture_output=True,text=True,encoding='utf-8')
    if proc.returncode: raise RuntimeError(proc.stderr)
    assembly=json.loads(proc.stdout)
    src=target/'src'
    modifications=[]
    p=src/'mcp/physics_gate.py'
    if p.exists():
        text=p.read_text(encoding='utf-8')
        start=text.index('    if n > 3000:\n',text.index('def compute_dmin('))
        end=text.index('    best = float("inf")\n',start)
        # Find the unindented pairwise loop after the optional NumPy block.
        end=text.index('    best = float("inf")\n    for i in range(len(coords)):',end)
        text=text[:start]+text[end:]
        text=text.replace('N>3000 用 numpy 分块,无 numpy 抽样 3000。','所有原子使用标准库逐对计算；大体系可能较慢。')
        p.write_text(text,encoding='utf-8',newline='\n')
        modifications.append('physics_gate.compute_dmin uses all atoms with stdlib O(n²), no NumPy sampling')
    if name in ('knowledge','evidence','gate'):
        pdf=src/'ukb/ukb/plugins/pdf_text.py'
        if pdf.exists():
            pdf.write_text('"""No bundled PDF parser; inject a trusted extractor explicitly."""\n'
                           'def available(): return False\n'
                           'def extract(data): return None\n',encoding='utf-8')
            modifications.append('automatic third-party PDF extraction disabled; injectable extractor remains supported')
    shutil.copyfile(ROOT/'tools/runtime.py',target/'run.py')
    shutil.copyfile(PACKAGES/name/'INTERFACE.md',target/'LEGACY-INTERFACE.md')
    shutil.copyfile(PACKAGES/name/'LICENSE',target/'LICENSE')
    shutil.copyfile(PACKAGES/name/'NOTICE',target/'NOTICE')
    shutil.copyfile(ROOT/'docs/MODULE-INTERFACE.md',target/'INTERFACE.md')
    meta={'module':name,'version':'0.2.0','protocol':'mdagent-stdio/1','python':'3.11+',
          'external_install_dependencies':[], 'embedded_legacy_components':assembly['modules'],
          'modifications':modifications,'source_version':'0.1.1'}
    (target/'MODULE.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # Original schemas and tests stay available in src. Exclude caches from inventory.
    files={p.relative_to(target).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
           for p in target.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo')}
    (target/'SHA256SUMS.json').write_text(json.dumps(dict(sorted(files.items())),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(name,len(files),'files',flush=True)
