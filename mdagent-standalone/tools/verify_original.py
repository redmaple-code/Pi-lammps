"""Run retained original unit tests without user/site packages in a temporary copy."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('module',choices=['knowledge','evidence','gate','consistency']);args=p.parse_args()
out=ROOT/'evidence/0.2.0';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='standalone-original-') as tmp:
    package=Path(tmp)/'package'
    shutil.copytree(ROOT/'packages/0.2.0'/args.module,package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    src=package/'src'
    configs={
        'knowledge':(src/'ukb/tests',src/'ukb','test_*.py'),
        'evidence':(src/'tests',src/'tests','test_evidence_v3_service.py'),
        'gate':(src/'tests',src/'tests','test_physics_gate.py'),
        'consistency':(src/'design-review-layer/tests',src/'design-review-layer/tests','test_consistency.py'),
    }
    start,top,pattern=configs[args.module]
    code="""import sys,unittest
sys.dont_write_bytecode=True
src=sys.argv[1]
sys.path[:0]=[src,src+'/mcp',src+'/evolution',src+'/ukb',src+'/design-review-layer']
suite=unittest.defaultTestLoader.discover(sys.argv[2],pattern=sys.argv[4],top_level_dir=sys.argv[3])
result=unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun else 1)
"""
    env={k:v for k,v in os.environ.items() if not k.startswith(('MDSIM_','MDAGENT_','MDPROJ_','REVIEW_','UKB_')) and k!='PYTHONPATH'}
    env.update(MDSIM_DATA_ROOT=str(Path(tmp)/'runtime'),MDPROJ_ROOT=str(Path(tmp)/'projects'))
    cmd=[sys.executable,'-I','-S','-c',code,str(src),str(start),str(top),pattern]
    proc=subprocess.run(cmd,cwd=src,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=900)
    text=proc.stdout+'\n'+proc.stderr
    (out/(args.module+'-original.log')).write_text(text,encoding='utf-8')
    match=re.search(r'Ran (\d+) tests?',text); skip=re.search(r'skipped=(\d+)',text)
    result={'module':args.module,'isolated_mode':'-I -S','returncode':proc.returncode,
            'tests':int(match.group(1)) if match else None,'skipped':int(skip.group(1)) if skip else 0,
            'scope':pattern,'ok':proc.returncode==0 and bool(match)}
    (out/(args.module+'-original.json')).write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result),flush=True)
    sys.exit(0 if result['ok'] else 1)
