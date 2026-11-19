import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import importlib.util

ROOT=Path(__file__).resolve().parents[1]
PACKAGES=ROOT/'packages/0.2.0'

def run(name, root, requests, write=False):
    cmd=[sys.executable,'-I','-S',str(PACKAGES/name/'run.py'),'--data-root',str(root),'--actor','test-agent']
    if write: cmd.append('--allow-write')
    p=subprocess.run(cmd,input='\n'.join(json.dumps(x,ensure_ascii=False) for x in requests)+'\n',
                     capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
    if p.returncode: raise AssertionError('worker failed: '+p.stderr[-3000:])
    try: return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    except ValueError as e: raise AssertionError('non-JSON protocol output: '+p.stdout[-1000:]) from e

class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mdagent-standalone-test-')
        self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def test_each_module_starts_without_site_packages(self):
        for name in ('knowledge','evidence','gate','consistency'):
            rows=run(name,self.root/name,[{'id':'1','operation':'describe'}])
            self.assertEqual(rows[0]['result']['module'],name)
            self.assertFalse(rows[0]['result']['writeEnabled'])
    def test_knowledge_create_query_and_audit(self):
        root=self.root/'knowledge'
        rows=run('knowledge',root,[{'id':'1','operation':'init'},{'id':'2','operation':'status'},
                                   {'id':'3','operation':'verify_audit'}],True)
        self.assertTrue(all(x['ok'] for x in rows),rows)
        self.assertTrue(rows[2]['result']['ok'])
        db=sqlite3.connect(root/'module-knowledge-audit.sqlite')
        self.assertEqual(db.execute('select count(*) from log').fetchone()[0],6)
        db.execute("update log set body='{}' where seq=1");db.commit();db.close()
        rows=run('knowledge',root,[{'id':'4','operation':'status'}])
        self.assertEqual(rows[0]['error']['code'],'AUDIT_UNAVAILABLE')
    def test_writes_are_disabled_by_default(self):
        rows=run('knowledge',self.root/'readonly',[{'id':'1','operation':'init'}])
        self.assertEqual(rows[0]['error']['code'],'WRITE_DISABLED')
        self.assertFalse((self.root/'readonly/ukb/ukb.sqlite').exists())
    def test_knowledge_proposal_remains_candidate(self):
        payload={'mode':'new','type_id':'rule','title':'Example rule',
                 'fields':{'statement':'Inspect the input.','severity':'soft','rationale':'Evidence','check':'Inspect'},
                 'tags':[{'facet':'topic','tag':'general'}],
                 'sources':[{'source_type':'knowledge_entry','title':'test','locator':'fixture'}],
                 'origin':{'kind':'task','ref':'task-test','summary_of_evidence':'test evidence'}}
        rows=run('knowledge',self.root/'proposal',[
            {'id':'1','operation':'init'}, {'id':'2','operation':'propose','input':{'payload':payload}},
            {'id':'3','operation':'catalog'}],True)
        self.assertTrue(rows[1]['ok'],rows)
        self.assertIn('task_id',rows[1]['result'])
        self.assertTrue(rows[2]['ok'],rows)
    def test_evidence_real_task_and_revision_conflict(self):
        intent={'action':'production','goals':[{'id':'goal-a','text':'inspect'}],
                'conditions':{},'unknowns':[],'source_refs':['request:a']}
        rows=run('evidence',self.root/'evidence',[
            {'id':'1','operation':'init','input':{'task_id':'task-a','intent':intent,'request_id':'create-a'}},
            {'id':'2','operation':'status','input':{'task_id':'task-a'}},
            {'id':'3','operation':'revise','input':{'task_id':'task-a','patch':{'decisions':{}},'expected_revision':999}}],True)
        self.assertTrue(rows[0]['ok'],rows);self.assertTrue(rows[1]['ok'],rows)
        self.assertEqual(rows[1]['result']['task_id'],'task-a')
        self.assertFalse(rows[2]['ok']);self.assertEqual(rows[2]['error']['code'],'REVISION_CONFLICT')
    def test_separate_processes_can_use_explicit_common_knowledge_root(self):
        root=self.root/'combined'
        created=run('knowledge',root,[{'id':'1','operation':'init'}],True)
        self.assertTrue(created[0]['ok'],created)
        intent={'action':'production','goals':[{'id':'goal-a','text':'inspect'}],
                'conditions':{},'unknowns':[],'source_refs':['request:a']}
        state=run('evidence',root,[{'id':'2','operation':'init',
                    'input':{'task_id':'combined-a','intent':intent}}],True)
        self.assertTrue(state[0]['ok'],state)
        self.assertEqual(state[0]['result']['task_id'],'combined-a')
    def test_actual_consistency_fixture(self):
        case=PACKAGES/'consistency/src/design-review-layer/cases/case1'
        rows=run('consistency',self.root/'consistency',[{'id':'1','operation':'inspect','input':{
            'intent':json.loads((case/'intent.json').read_text(encoding='utf-8')),
            'infile_text':(case/'in.case').read_text(encoding='utf-8')}}])
        self.assertTrue(rows[0]['ok'],rows)
        self.assertGreaterEqual(len(rows[0]['result']['checks']),9)
        self.assertIn(rows[0]['result']['verdict'],('PASS','REVIEW','FAIL','NEEDS_REVISION'))
    def test_gate_measure_uses_all_atoms_without_numpy(self):
        coords=[[i*2,0,0] for i in range(3000)]+[[0.000001,0,0]]
        rows=run('gate',self.root/'gate',[{'id':'1','operation':'measure','input':{'parsed':{'coords':coords,'box':None}}}])
        self.assertTrue(rows[0]['ok'],rows)
        self.assertAlmostEqual(rows[0]['result']['dmin_angstrom'],0.000001)
        self.assertEqual(rows[0]['result']['note'],'')
    def test_action_injection_and_unavailable_operation_rejected(self):
        rows=run('evidence',self.root/'security',[
            {'id':'1','operation':'status','input':{'action':'init'}},
            {'id':'2','operation':'execute'}])
        self.assertEqual(rows[0]['error']['code'],'INVALID_INPUT')
        self.assertEqual(rows[1]['error']['code'],'ACTION_UNAVAILABLE')
    def test_host_client_can_attach_and_detach_modules(self):
        spec=importlib.util.spec_from_file_location('standalone_client',ROOT/'tools/client.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with module.ModuleProcess(PACKAGES/'knowledge',self.root/'client','host-test') as client:
            response=client.call('describe',request_id='attach-1')
            self.assertEqual(response['result']['module'],'knowledge')
            self.assertFalse(response['result']['writeEnabled'])
    def test_audit_commit_failure_after_write_is_unknown(self):
        script='''import importlib.util,sys
spec=importlib.util.spec_from_file_location("worker",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
old=m.Audit.append
def failing(self,event):
    if event.get("phase")=="completed": raise OSError("simulated audit failure")
    return old(self,event)
m.Audit.append=failing
sys.argv=[sys.argv[1],"--data-root",sys.argv[2],"--actor","test-agent","--allow-write"]
m.main()
'''
        root=self.root/'audit-failure'
        proc=subprocess.run([sys.executable,'-I','-S','-c',script,str(PACKAGES/'knowledge/run.py'),str(root)],
                            input='{"id":"1","operation":"init"}\n',capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(proc.returncode,0,proc.stderr)
        response=json.loads(proc.stdout.strip())
        self.assertEqual(response['error']['code'],'AUDIT_UNAVAILABLE')
        self.assertEqual(response['error']['sideEffectState'],'unknown')
        self.assertTrue((root/'ukb/ukb.sqlite').is_file())

if __name__=='__main__': unittest.main(verbosity=2)
