"""Validate HR-1 document links, source references, task mapping and observed counts."""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
trace = json.loads((ROOT/'findings.json').read_text(encoding='utf-8'))
source = (ROOT/trace['source_package']).resolve()
errors = []
checks = []

def check(ok, label):
    checks.append({'check':label,'passed':bool(ok)})
    if not ok:
        errors.append(label)

for doc in ROOT.glob('*.md'):
    for target in re.findall(r'\]\(([^)]+)\)',doc.read_text(encoding='utf-8')):
        if '://' in target or target.startswith('#'):
            continue
        target = unquote(target.split('#',1)[0].strip('<>'))
        check((doc.parent/target).exists(),f'link:{doc.name}:{target}')

work = (ROOT/'02-work-items.md').read_text(encoding='utf-8')
review = (ROOT/'00-review.md').read_text(encoding='utf-8')
tasks_text = (ROOT.parent/'module-development/tasks.json').read_text(encoding='utf-8')
existing_tasks = set(re.findall(r'DEV-\d{2}-\d+',tasks_text))
probe_ids = {x['id'] for x in json.loads((ROOT/'evidence/probes.json').read_text(encoding='utf-8'))['probes']}
check(len(trace['findings'])==12,'12 findings')
check(set(re.findall(r'### HR-W\d{2}',work)) == {'### HR-W%02d'%i for i in range(1,9)},'8 work items')
check(set(re.findall(r'HR-ACC-\d{2}',work)) == {'HR-ACC-%02d'%i for i in range(1,22)},'21 acceptance cases')
for row in trace['findings']:
    check(row['id']+' / '+row['priority'] in review,'review:'+row['id'])
    check(row['implementation']=='notImplemented' and row['fix_verification']=='notRun','honest-status:'+row['id'])
    for task in row['existing_tasks']:
        check(task in existing_tasks,'task:'+task)
    for module in row['modules']:
        check((ROOT.parent/'module-development/modules'/(module+'.md')).is_file(),'module:'+module)
    for target in row['work_items']+row['acceptance']:
        check(target in work,'work-ref:'+target)
    for pid in row['probes']:
        check(pid in probe_ids,'probe:'+pid)
    for ref in row['sources']:
        p = source/ref['path']
        check(p.is_file() and 0 < ref['line'] <= len(p.read_text(encoding='utf-8').splitlines()),'source:'+ref['path'])

manifest = json.loads((source/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
for rel, entry in manifest['files'].items():
    p = source/rel
    check(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==entry['sha256'],'source-hash:'+rel)
results = json.loads((ROOT/'evidence/rerun/RESULTS.json').read_text(encoding='utf-8'))
check(len(results['suites'])==21 and results['failures']==1 and not results['all_ok'],'full-rerun-is-not-all-green')
summary={'revision':'HR-1','checks':len(checks),'passed':sum(c['passed'] for c in checks),'errors':errors,
         'scope':'document structure/source bytes only; does not close product findings',
         'findings':12,'work_items':8,'acceptance_cases':21,
         'priority_counts':{p:sum(r['priority']==p for r in trace['findings']) for p in ('P1','P2')}}
(ROOT/'evidence/document-validation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
