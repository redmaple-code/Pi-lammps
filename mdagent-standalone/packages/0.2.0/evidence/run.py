"""Single-module stdio adapter. Launch with Python -I; only stdlib is required."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

sys.dont_write_bytecode = True
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='strict')

HERE = Path(__file__).resolve().parent
SRC = HERE / 'src'
MANIFEST = json.loads((HERE / 'MODULE.json').read_text(encoding='utf-8'))
MODULE = MANIFEST['module']
for path in (SRC, SRC/'mcp', SRC/'evolution', SRC/'ukb', SRC/'design-review-layer'):
    if path.is_dir(): sys.path.insert(0, str(path))

class Failure(Exception):
    def __init__(self, code, side_effect='none'):
        self.code, self.side_effect = code, side_effect

def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()

class Audit:
    def __init__(self, file):
        self.db = sqlite3.connect(file, isolation_level=None, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS log(seq INTEGER PRIMARY KEY, prev TEXT NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL)')

    def verify(self):
        prior='genesis'; seq=0
        for row in self.db.execute('SELECT seq,prev,body,hash FROM log ORDER BY seq'):
            seq+=1
            if row[0]!=seq or row[1]!=prior or digest({'seq':seq,'prev':prior,'body':row[2]})!=row[3]: return False
            prior=row[3]
        return True

    def append(self, event):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if not self.verify(): raise RuntimeError('audit corrupt')
            row=self.db.execute('SELECT seq,hash FROM log ORDER BY seq DESC LIMIT 1').fetchone()
            seq=(row[0] if row else 0)+1; prior=row[1] if row else 'genesis'
            body=encode(event); checksum=digest({'seq':seq,'prev':prior,'body':body})
            self.db.execute('INSERT INTO log VALUES(?,?,?,?)',(seq,prior,body,checksum))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise

OPS = {
    'knowledge': {'init':'write','overview':'read','catalog':'read','read':'read','status':'read','verify_audit':'read','propose':'write'},
    'evidence': {'status':'read','evaluate':'read','retrieve':'read','init':'write','revise':'write','plan':'write',
                 'evidence':'write','proof':'write','advance':'write','resolve':'write','measure':'write','complete':'write'},
    'gate': {'measure':'read','project_list':'read','task_status':'read','intent_view':'read'},
    'consistency': {'inspect':'read'},
}

def needed(value, expected, name):
    if not isinstance(value, expected): raise Failure('INVALID_'+name.upper())
    return value

def knowledge(action, data, root, actor):
    from ukb.api import UKB
    from ukb.core.errors import UKBError
    store = root/'ukb'
    if action=='init':
        kb=UKB.init(str(store),actor=actor); kb.close(); os.environ['MDAGENT_KB']='ukb'; return {'initialized':True}
    if not (store/'ukb.sqlite').is_file(): raise Failure('STORE_NOT_INITIALIZED')
    kb=UKB(str(store))
    try:
        if action=='overview': return kb.overview('agent',facet=data.get('facet'),within=data.get('within'),parent=data.get('parent'))
        if action=='catalog': return kb.catalog('agent',scope=data.get('scope'),types=data.get('types'),limit=data.get('limit'))
        if action=='read': return kb.read('agent',scope=data.get('scope'),entry_refs=data.get('entry_refs'),types=data.get('types'),cursor=data.get('cursor'),view=data.get('view','full'))
        if action=='status': return kb.status()
        if action=='verify_audit': return kb.verify_audit()
        if action=='propose': return kb.propose(needed(data.get('payload'),dict,'payload'),'mcp',actor)
    except UKBError as exc:
        # Some rejection paths record an invalid intake. Treat write errors as unknown.
        raise Failure(str(exc.code), 'unknown' if action=='propose' else 'none') from exc
    finally: kb.close()
    raise Failure('ACTION_UNAVAILABLE')

def evidence(action, data, root):
    import evidence_api
    for raw in data.get('knowledge_dirs', []):
        try: Path(raw).resolve().relative_to(root)
        except (ValueError,TypeError): raise Failure('UNSAFE_PATH')
    result=evidence_api.dispatch({'action':action,**data},str(root))
    if isinstance(result,dict) and result.get('error'):
        code=str(result['error'])
        raise Failure(code if re.fullmatch(r'[A-Za-z0-9_-]{1,80}',code) else 'EVIDENCE_ERROR',
                      'unknown' if OPS['evidence'][action]=='write' else 'none')
    return result

def gate(action, data):
    if action=='measure':
        import physics_gate
        parsed=needed(data.get('parsed'),dict,'parsed')
        coords=needed(parsed.get('coords'),list,'coords')
        if len(coords)>10000: raise Failure('RESOURCE_LIMIT')
        try:
            distance,note=physics_gate.compute_dmin(parsed)
            return {'dmin_angstrom':distance,'note':note,'method':'exact-orthogonal-minimum-image',
                    'scope':'measurement-only'}
        except (TypeError,ValueError,KeyError,OverflowError) as exc: raise Failure('INVALID_GEOMETRY') from exc
    import md_proj_mcp
    return {'project_list':md_proj_mcp.list_projects,'task_status':md_proj_mcp.task_status,
            'intent_view':md_proj_mcp.intent_view}[action](data)

def consistency(action, data):
    from review_layer.consistency import build_consistency
    intent=needed(data.get('intent'),dict,'intent'); text=needed(data.get('infile_text'),str,'infile_text')
    if not text or len(text)>2_000_000: raise Failure('INVALID_INFILE_TEXT')
    items=build_consistency(intent,text)
    allowed={'ok','questionable','unproven','invalid','plan_conflict'}
    ids=[x.id for x in items]
    if len(ids)!=len(set(ids)): raise Failure('DUPLICATE_CHECK_ID')
    verdicts=[x.verdict for x in items]
    if any(v not in allowed for v in verdicts): raise Failure('UNKNOWN_VERDICT')
    verdict='FAIL' if 'invalid' in verdicts else 'NEEDS_REVISION' if 'plan_conflict' in verdicts else \
            'REVIEW' if not items or any(v in ('questionable','unproven') for v in verdicts) else 'PASS'
    return {'verdict':verdict,'checks':[asdict(x) for x in items],
            'limitations':['static check only; not scientific validation or execution authorization',
                           'complete include/variable dependency coverage not guaranteed']}

def main():
    parser=argparse.ArgumentParser(description='MDagent standalone module stdio protocol')
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--actor',required=True,help='host-authenticated opaque actor ID; no personal data')
    parser.add_argument('--allow-write',action='store_true')
    parser.add_argument('--project-root',type=Path)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',args.actor): parser.error('invalid actor')
    root=args.data_root.resolve(); root.mkdir(parents=True,exist_ok=True)
    os.environ['MDSIM_DATA_ROOT']=str(root)
    os.environ['MDAGENT_UKB_ROOT']=str(root/'ukb')
    os.environ['REVIEW_UKB_ROOT']=str(root/'ukb')
    os.environ['MDAGENT_KB']='ukb' if (root/'ukb'/'ukb.sqlite').is_file() else 'legacy'
    os.environ['MDPROJ_ROOT']=str(args.project_root.resolve() if args.project_root else root/'projects')
    audit=Audit(root/('module-'+MODULE+'-audit.sqlite'))
    for line in sys.stdin:
        req=None
        try:
            req=json.loads(line)
            if not isinstance(req,dict) or set(req)-{'id','operation','input'}: raise Failure('INVALID_REQUEST')
            rid=req.get('id'); op=req.get('operation'); data=req.get('input',{})
            if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',rid): raise Failure('INVALID_ID')
            if op=='describe':
                response={'id':rid,'ok':True,'result':{'module':MODULE,'version':MANIFEST['version'],
                    'protocol':'mdagent-stdio/1','operations':OPS[MODULE],'writeEnabled':args.allow_write}}
            else:
                if op not in OPS[MODULE]: raise Failure('ACTION_UNAVAILABLE')
                if not isinstance(data,dict) or 'action' in data: raise Failure('INVALID_INPUT')
                effect=OPS[MODULE][op]
                entry={'call_id':uuid.uuid4().hex,'module':MODULE,'operation':op,'actor_hash':digest(args.actor),
                       'request_hash':digest({'id':rid,'input':data}),'effect':effect}
                try: audit.append({**entry,'phase':'started','time':time.time_ns()})
                except Exception: raise Failure('AUDIT_UNAVAILABLE')
                try:
                    if effect=='write' and not args.allow_write: raise Failure('WRITE_DISABLED')
                    if MODULE=='knowledge': result=knowledge(op,data,root,args.actor)
                    elif MODULE=='evidence': result=evidence(op,data,root)
                    elif MODULE=='gate': result=gate(op,data)
                    else: result=consistency(op,data)
                    try: audit.append({**entry,'phase':'completed','result_hash':digest(result),'time':time.time_ns()})
                    except Exception: raise Failure('AUDIT_UNAVAILABLE','unknown' if effect=='write' else 'none')
                    response={'id':rid,'ok':True,'result':result}
                except Failure as exc:
                    try: audit.append({**entry,'phase':'failed','code':exc.code,'side_effect':exc.side_effect,'time':time.time_ns()})
                    except Exception: raise Failure('AUDIT_UNAVAILABLE','unknown' if effect=='write' else 'none')
                    raise
                except Exception as exc:
                    state='unknown' if effect=='write' else 'none'
                    try: audit.append({**entry,'phase':'failed','code':'INTERNAL_ERROR','side_effect':state,'time':time.time_ns()})
                    except Exception: pass
                    raise Failure('INTERNAL_ERROR',state) from exc
        except Failure as exc:
            response={'id':req.get('id') if isinstance(locals().get('req'),dict) else None,'ok':False,
                      'error':{'code':exc.code,'sideEffectState':exc.side_effect,'retryable':False}}
        except Exception:
            response={'id':None,'ok':False,'error':{'code':'INVALID_REQUEST','sideEffectState':'none','retryable':False}}
        print(encode(response),flush=True)

if __name__=='__main__': main()
