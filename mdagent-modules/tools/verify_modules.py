"""Exercise each assembled distribution in a disposable isolated runtime."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / 'packages/0.1.1'
SPLIT = ROOT / 'tools/split_handover.py'

parser = argparse.ArgumentParser()
parser.add_argument('module', choices=['knowledge','evidence','gate','consistency'])
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
env = {k:v for k,v in os.environ.items() if not k.startswith(('MDSIM_','MDAGENT_','MDPROJ_','REVIEW_','UKB_')) and k != 'PYTHONPATH'}
env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
rows=[]
with tempfile.TemporaryDirectory(prefix='mdagent-modular-') as temp:
    root = Path(temp); assembly = root/'assembly'
    env.update(MDSIM_DATA_ROOT=str(root/'runtime'), MDPROJ_ROOT=str(root/'projects'))
    cmd = [sys.executable, str(SPLIT), 'compose', '--packages', str(PACKAGES), '--out', str(assembly), args.module]
    prep = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
    if prep.returncode: raise RuntimeError('isolated module assembly failed: ' + prep.stderr)
    src=assembly/'src'
    tests=[]
    if args.module == 'knowledge':
        # Legacy compatibility tests also exercise v3 evidence and therefore need
        # the combined assembly; the UKB engine itself is tested independently.
        combined = root/'combined'
        extra = subprocess.run([sys.executable, str(SPLIT), 'compose', '--packages', str(PACKAGES),
                                '--out', str(combined), 'evidence'], env=env, capture_output=True,
                               text=True, encoding='utf-8', timeout=120)
        if extra.returncode: raise RuntimeError('combined assembly failed: ' + extra.stderr)
        tests=[('ukb-package',[sys.executable,'-m','unittest','discover','-s','tests','-t','.'],src/'ukb'),
               ('knowledge-evidence-integration',[sys.executable,'-m','unittest','discover','-s','tests','-t','tests','-p','test_kb_*.py'],combined/'src')]
    elif args.module == 'evidence':
        tests=[('evidence-v3',[sys.executable,'-m','unittest','discover','-s','tests','-t','tests','-p','test_evidence_v3_*.py'],src)]
    elif args.module == 'gate':
        for name in ['test_stage_gate.py','test_stage_state.py','test_assessment_v2.py','test_config_check.py',
                     'test_deep_review.py','test_evidence_grade.py','test_design_review.py','test_physics_gate.py','test_md_proj_mcp.py']:
            tests.append((name[:-3],[sys.executable,'-m','unittest','discover','-s','tests','-t','tests','-p',name],src))
        tests.append(('flow-gate',['node','tests/test_flow_gate.mjs'],src))
    else:
        layer=src/'design-review-layer'
        tests=[('consistency-unit',[sys.executable,'-m','unittest','discover','-s','tests','-p','test_consistency.py'],layer),
               ('template',[sys.executable,'-m','review_layer.template_check'],layer),
               ('fixture-cli',[sys.executable,'-m','review_layer.cli','--intent','cases/case1/intent.json','--infile','cases/case1/in.case','--fixture','--out',str(root/'reports')],layer),
               ('summary-guard',[sys.executable,'-c',
                 'from review_layer.models import ReviewReport,ConsistencyItem;from review_layer.review import summarize;'
                 'r=ReviewReport("x","x",[],[],[],"UNKNOWN","");assert summarize(r).verdict=="REVIEW";'
                 'r.consistency=[ConsistencyItem("x","x","x","x",verdict="unsupported")];assert summarize(r).verdict=="REVIEW"'],layer)]
    for name, command, cwd in tests:
        start=time.monotonic()
        try:
            proc=subprocess.run(command,cwd=cwd,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=900)
            output=proc.stdout+'\n'+proc.stderr
            (args.out/(args.module+'-'+name+'.log')).write_text(output,encoding='utf-8')
            match=re.search(r'Ran (\d+) tests?',output)
            skip=re.search(r'skipped=(\d+)',output)
            tests_found=int(match.group(1)) if match else None
            is_unittest='unittest' in command
            ok=proc.returncode==0 and (not is_unittest or bool(tests_found))
            row={'name':name,'returncode':proc.returncode,'ok':ok,'tests':tests_found,
                 'skipped':int(skip.group(1)) if skip else 0,'seconds':round(time.monotonic()-start,1),
                 'log':args.module+'-'+name+'.log'}
        except (subprocess.TimeoutExpired,FileNotFoundError) as error:
            row={'name':name,'returncode':None,'ok':False,'tests':None,'skipped':0,'error':type(error).__name__}
        rows.append(row); print(json.dumps(row),flush=True)
result={'module':args.module,'assembly':json.loads(prep.stdout),'python':sys.version.split()[0],
        'environment':'temporary isolated data root; Python UTF-8 mode', 'suites':rows,'ok':all(r['ok'] for r in rows)}
(args.out/(args.module+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
sys.exit(0 if result['ok'] else 1)
