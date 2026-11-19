"""Verify actual stdio initialize/tools/list surfaces from an isolated assembly."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
env={k:v for k,v in os.environ.items() if not k.startswith(('MDSIM_','MDAGENT_','MDPROJ_','REVIEW_','UKB_')) and k!='PYTHONPATH'}
env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
out=ROOT/'evidence/0.1.1/mcp.json'
rows=[]
with tempfile.TemporaryDirectory(prefix='mdagent-mcp-') as temp:
    root=Path(temp); assembly=root/'assembly'; store=root/'runtime'/'ukb'
    env.update(MDSIM_DATA_ROOT=str(root/'runtime'), MDPROJ_ROOT=str(root/'projects'), MDAGENT_KB='ukb',
               MDAGENT_UKB_ROOT=str(store),REVIEW_UKB_ROOT=str(store))
    p=subprocess.run([sys.executable,str(ROOT/'tools/split_handover.py'),'compose','--packages',str(ROOT/'packages/0.1.1'),
                      '--out',str(assembly),'evidence','consistency'],env=env,capture_output=True,text=True,encoding='utf-8',timeout=120)
    if p.returncode: raise RuntimeError('compose failed')
    src=assembly/'src'; ukb=src/'ukb'
    bootstrap=subprocess.run([sys.executable,'-c','from ukb.api import UKB; import sys; k=UKB.init(sys.argv[1]); k.close()',str(store)],
                             cwd=ukb,env=env,capture_output=True,text=True,encoding='utf-8',timeout=120)
    if bootstrap.returncode: raise RuntimeError('isolated UKB init failed')
    payload='\n'.join(json.dumps(x) for x in [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
        {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}])+'\n'
    for name,cwd,command,required in [
        ('knowledge',ukb,[sys.executable,'-m','ukb.mcp_server','--root',str(store),'--agent','writer'],['ukb_overview','ukb_catalog','ukb_read']),
        ('gate',src,[sys.executable,'mcp/md_proj_mcp.py'],['intent_view','validate_intent','confirm_intent']),
        ('evidence',src,[sys.executable,'mcp/md_mcp.py'],['sim_evidence','sim_exec'])]:
        p=subprocess.run(command,cwd=cwd,env=env,input=payload,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
        try:
            replies={r['id']:r for line in p.stdout.splitlines() if line.strip() for r in [json.loads(line)] if 'id' in r}
            tools=replies[2]['result']['tools']; names={t['name'] for t in tools}
            good=p.returncode==0 and 'result' in replies[1] and set(required)<=names
            row={'module':name,'returncode':p.returncode,'tools':len(tools),'required_present':sorted(set(required)&names),'ok':good}
        except (ValueError,KeyError,TypeError): row={'module':name,'returncode':p.returncode,'ok':False,'error':'invalid MCP response'}
        rows.append(row); print(json.dumps(row),flush=True)
out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps({'mode':'isolated stdio; no external model or solver','results':rows,'ok':all(x['ok'] for x in rows)},indent=2)+'\n',encoding='utf-8')
sys.exit(0 if all(x['ok'] for x in rows) else 1)
