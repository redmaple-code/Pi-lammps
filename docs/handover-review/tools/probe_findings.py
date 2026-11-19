"""Small, isolated reproductions of handoff review findings; no production calls."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('package', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    p = args.package.resolve()
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(p/'src/mcp'), str(p/'src'), str(p/'src/design-review-layer')]
    rows = []
    with tempfile.TemporaryDirectory(prefix='mdhandoff-probes-') as temp:
        root = Path(temp).resolve()
        with patch.dict(os.environ, {'MDPROJ_ROOT': str(root), 'MDSIM_DATA_ROOT': str(root/'data')}, clear=False):
            import md_proj_mcp
            import design_review
            from review_layer.models import ReviewReport, ConsistencyItem
            from review_layer.review import summarize
            project = root/'project'
            project.mkdir()
            (project/'foo_20260101_000000').mkdir()
            (project/'foo_bar_20260102_000000').mkdir()
            with patch.object(md_proj_mcp, 'project_root', return_value=str(root)):
                found = md_proj_mcp._find_run_dir('project', 'foo')
                rows.append({'id': 'PROBE-01', 'subject': 'case prefix collision',
                             'observed': Path(found).name, 'defect_reproduced': Path(found).name.startswith('foo_bar')})
                case = project/'foo_20260101_000000'
                target = case/'intent.json'
                target.write_text('{"preserve":true}', encoding='utf-8')
                before = target.read_bytes()
                def interrupted_dump(value, stream, **kwargs):
                    stream.write('{')
                    raise OSError('simulated write interruption')
                try:
                    with patch.object(md_proj_mcp.json, 'dump', side_effect=interrupted_dump):
                        md_proj_mcp._save_intent(str(case), {'new': True})
                except OSError:
                    pass
                rows.append({'id': 'PROBE-02', 'subject': 'interrupted intent save',
                             'observed': target.read_text(encoding='utf-8'),
                             'defect_reproduced': target.read_bytes() != before})
                (project/'auth_20260101_000000').mkdir()
                authfile = project/'auth_20260101_000000/intent.json'
                authfile.write_text(json.dumps({'status':'DESIGNED','decisions':[]}), encoding='utf-8')
                confirmed = md_proj_mcp.confirm_intent({'project':'project','case_id':'auth',
                                                      'confirm_quote':'synthetic probe text; no human decision'})
                rows.append({'id':'PROBE-08','subject':'self-reported approval quote',
                             'observed':confirmed.get('status'),
                             'defect_reproduced':confirmed.get('status') == 'CONFIRMED'})
            for name, items in [('empty', []), ('unknown', [ConsistencyItem('x','x','x','x',verdict='unsupported')])]:
                report = ReviewReport('probe', '2026-11-17', [], items, [], 'UNKNOWN', '')
                verdict = summarize(report).verdict
                rows.append({'id': 'PROBE-03' if name=='empty' else 'PROBE-04',
                             'subject': name+' consistency list', 'observed': verdict,
                             'defect_reproduced': verdict == 'PASS'})
            marker = 'REVIEW_TAIL_MUST_NOT_DISAPPEAR'
            brief = design_review.build_brief({'description':'x'*6500,'key_params':{'critical':marker}},
                                             {}, [], {'entries':[], 'checklists':[]})
            rows.append({'id':'PROBE-05','subject':'review input truncation',
                         'observed': {'tail_present': marker in brief}, 'defect_reproduced':marker not in brief})
            from evolution.kb import adapter
            from review_layer import ukb_store
            with patch.dict(os.environ, {}, clear=True):
                os.environ['MDSIM_DATA_ROOT'] = str(root/'custom')
                a = Path(adapter.ukb_root(adapter.data_root())).resolve()
                b = ukb_store.root().resolve()
                rows.append({'id':'PROBE-06','subject':'MDSIM_DATA_ROOT-only resolution',
                             'observed':{'adapter_uses_custom':a == root/'custom/ukb', 'same_store':a == b},
                             'defect_reproduced':a != b})
            spec = importlib.util.spec_from_file_location('handoff_runner', p/'tools/run_verification.py')
            runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(runner)
            captured = []
            class Result:
                returncode = 0
            def capture(command, **kwargs):
                captured.append(kwargs['env'])
                return Result()
            sentinel = str(root/'outside-default-store')
            with patch.dict(os.environ, {'REVIEW_UKB_ROOT': sentinel}), patch.object(runner.subprocess, 'run', side_effect=capture):
                provisioning = runner._provision_ukb(root/'expected-store')
            rows.append({'id':'PROBE-07','subject':'runner inherits external UKB root',
                         'observed':{'inherits_external_root':all(e['REVIEW_UKB_ROOT']==sentinel for e in captured),
                                     'admin_filename':Path(provisioning['REVIEW_UKB_ADMIN_FILE']).name},
                         'defect_reproduced':all(e['REVIEW_UKB_ROOT']==sentinel for e in captured)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'scope':'isolated behavioral probes; no solver/model calls', 'probes':rows}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
