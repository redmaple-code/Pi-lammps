"""Build the location-only issue index; Markdown remains the design authority."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROWS = [
    ('01','P1','数据根与初始化', ['01','06'], ['17','01','12'], ['01','13'], ['06'],
     [('src/evolution/kb/adapter.py',117),('src/design-review-layer/review_layer/ukb_store.py',57),('README.md',80)]),
    ('02','P1','验证隔离与凭据清理', ['01'], ['17'], ['02','03'], ['07'],
     [('tools/run_verification.py',60)]),
    ('03','P1','算例前缀串案', ['02'], ['08','10'], ['04'], ['01'],
     [('src/mcp/md_proj_mcp.py',135)]),
    ('04','P1','方案原子提交与审计', ['02'], ['08'], ['05','06'], ['02'],
     [('src/mcp/md_proj_mcp.py',171),('src/mcp/md_proj_mcp.py',105)]),
    ('05','P1','审阅完整覆盖', ['03'], ['04','05','06'], ['07','08'], ['05'],
     [('src/mcp/design_review.py',234)]),
    ('06','P1','空或未知检查判定', ['04'], ['10','04'], ['09','10'], ['03','04'],
     [('src/design-review-layer/review_layer/review.py',18)]),
    ('07','P1','可信人工确认', ['05'], ['01','04','15'], ['11','12'], ['08'],
     [('src/mcp/md_proj_mcp.py',638),('src/mcp/md_proj_mcp.py',1054)]),
    ('08','P2','状态与action契约', ['06'], ['01','02','08','11','16','19'], ['13','14','15'], [],
     [('src/mcp/md_mcp.py',883),('src/evolution/stage_state.py',34),('src/evolution/evidence_v3/store.py',208),('src/evolution/evidence_v3/service.py',114),('src/mcp/evidence_api.py',92)]),
    ('09','P2','知识降级与能力披露', ['07'], ['12','06','19'], ['16'], [],
     [('src/design-review-layer/review_layer/common_issues.py',14),('src/design-review-layer/review_layer/discovery.py',30),('src/design-review-layer/review_layer/sources.py',93)]),
    ('10','P1','完整备份恢复', ['07'], ['12','16'], ['17','18'], [],
     [('src/ukb/ukb/api.py',184),('src/ukb/ukb/core/maintenance.py',106)]),
    ('11','P2','测试口径与编码可移植性', ['01','08'], ['17'], ['03','20','21'], [],
     [('verification/RESULTS.json',1),('tools/run_verification.py',35),('src/tests/test_md_mcp_contract.py',148),('src/mcp/md_mcp.py',21)]),
    ('12','P2','发布来源依赖与许可', ['08'], ['16','17','18'], ['19'], [],
     [('PACKAGE-MANIFEST.json',1),('PACKAGING-NOTES.md',1),('tools/package_manifest.py',87),('NOTICE',1),('SANITIZATION.md',1)]),
]

out = {'revision':'HR-1','date':'2026-11-17','source_zip_sha256':'e7fbae3489c45985cace5cbe9dc03c3ffb4837799828889227f27b57531832ec',
       'source_package':'../../source-snapshot/mdagent-handover-review-20261117/MDagent-subsystems-handover-20261117',
       'design_authority':'01-development-design.md','findings':[]}
for num, priority, title, work, modules, acceptance, probes, sources in ROWS:
    out['findings'].append({'id':'HR-'+num,'priority':priority,'title':title,
        'status':'open','implementation':'notImplemented','fix_verification':'notRun',
        'review':'00-review.md','work_items':['HR-W'+v for v in work],
        'modules':['MOD-'+v for v in modules],
        'existing_tasks':['DEV-'+v+'-'+str(n) for v in modules for n in range(1,4)],
        'acceptance':['HR-ACC-'+v for v in acceptance],
        'probes':['PROBE-'+v for v in probes],
        'basis':'behavioral-reproduction-and-source' if probes else 'source-and-document-review',
        'sources':[{'path':f,'line':line} for f,line in sources]})
(ROOT/'findings.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('wrote findings.json:',len(out['findings']),'findings')
