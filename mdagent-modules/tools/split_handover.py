"""Split the exact handoff source into attributed, composable source distributions.

No runtime database, token, reports, upstream logs or local configuration are copied.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'source-snapshot/mdagent-handover-review-20261117/MDagent-subsystems-handover-20261117'
NAMES = ('shared', 'knowledge', 'evidence', 'gate', 'consistency')
DEPENDENCIES = {
    'shared': [], 'knowledge': ['shared'], 'evidence': ['shared', 'knowledge', 'gate'],
    'gate': ['shared', 'knowledge'], 'consistency': ['shared'],
}

PATCHES = {
    'src/design-review-layer/review_layer/cli.py': (
        'sys.exit(0 if report.verdict == "OK" else 1)',
        'sys.exit(0 if report.verdict == "PASS" else 1)',
        'Correct CLI exit for PASS'),
    'src/design-review-layer/review_layer/review.py': (
        'elif any(i.verdict in ("questionable", "unproven") for i in items):',
        'elif not items or any(i.verdict not in ("ok", "questionable", "unproven", "invalid", "plan_conflict") for i in items) or any(i.verdict in ("questionable", "unproven") for i in items):',
        'Empty or unknown consistency verdict cannot PASS'),
}

def owner(rel: str) -> str:
    if rel.startswith(('src/ukb/', 'src/evolution/kb/', 'src/tools/kb/', 'src/data/domain/')):
        return 'knowledge'
    if rel.startswith(('src/evolution/evidence_v3/', 'src/mcp/evidence_', 'src/mcp/human_authority.py', 'src/tools/human_approve.py', 'src/webui/')):
        return 'evidence'
    if rel.startswith(('src/design-review-layer/',)):
        return 'consistency'
    gate_exact = {'stage_gate.py', 'stage_state.py', 'surface.py', 'assessment.py', 'content_check.py',
                  'config_check.py', 'deep_review.py', 'evidence_grade.py'}
    if rel.startswith('src/evolution/') and rel.split('/')[-1] in gate_exact:
        return 'gate'
    if rel.startswith(('src/mcp/md_proj_mcp.py', 'src/mcp/mdproj_config.json.example', 'src/mcp/design_review.py',
                       'src/mcp/physics_gate.py', 'src/mcp/review_experience/', 'src/dsh-plugins/')):
        return 'gate'
    if rel.startswith('src/tests/'):
        n = rel.rsplit('/', 1)[-1]
        if n.startswith('test_evidence_v3_') or n == 'test_human_authority.py': return 'evidence'
        if n.startswith('test_kb_'): return 'knowledge'
        if n in {'test_stage_gate.py', 'test_stage_state.py', 'test_assessment_v2.py', 'test_config_check.py',
                 'test_deep_review.py', 'test_evidence_grade.py', 'test_design_review.py', 'test_md_proj_mcp.py',
                 'test_physics_gate.py', 'test_flow_gate.mjs', 'e2e_dual_gate_demo.py'}: return 'gate'
    return 'shared'

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def split(out: Path):
    out.mkdir(parents=True, exist_ok=False)
    original = json.loads((SOURCE / 'PACKAGE-MANIFEST.json').read_text(encoding='utf-8'))
    rows = {n: {} for n in NAMES}
    for rel, meta in sorted(original['files'].items()):
        if not rel.startswith('src/'): continue
        # Source zip is the only input; do not copy generated runtime data.
        source = SOURCE / rel
        if sha(source) != meta['sha256']: raise ValueError('upstream digest mismatch: ' + rel)
        name = owner(rel)
        target = out / name / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        changes = []
        if rel in PATCHES:
            old, new, description = PATCHES[rel]
            if raw.count(old.encode()) != 1: raise ValueError('patch context mismatch: ' + rel)
            raw = raw.replace(old.encode(), new.encode(), 1)
            changes.append(description)
        target.write_bytes(raw)
        rows[name][rel] = {'sha256': sha(target), 'upstream_sha256': meta['sha256'],
                           'upstream_source': meta.get('source', 'unknown'), 'changes': changes}
    for name in NAMES:
        folder = out / name
        for file in ('LICENSE', 'NOTICE'):
            shutil.copyfile(SOURCE / file, folder / file)
        spec = {'name': name, 'version': '0.1.1', 'layout': 'handoff-src-overlay/1',
                'requires': DEPENDENCIES[name], 'source_zip_sha256': 'e7fbae3489c45985cace5cbe9dc03c3ffb4837799828889227f27b57531832ec',
                'files': rows[name]}
        (folder / 'manifest.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        shutil.copyfile(ROOT / 'docs' / (name + '.md'), folder / 'INTERFACE.md')
    print(json.dumps({k: len(v) for k, v in rows.items()}, ensure_ascii=False))

def compose(packages: Path, out: Path, names: list[str]):
    selected = set(names)
    pending = list(names)
    while pending:
        name = pending.pop()
        if name not in NAMES: raise ValueError('unknown module: ' + name)
        meta = json.loads((packages / name / 'manifest.json').read_text(encoding='utf-8'))
        if meta['name'] != name or meta['layout'] != 'handoff-src-overlay/1': raise ValueError('module identity mismatch')
        for required in meta['requires']:
            if required not in selected: selected.add(required); pending.append(required)
    out.mkdir(parents=True, exist_ok=False)
    written = {}
    for name in NAMES:
        if name not in selected: continue
        package = packages / name
        spec = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
        for rel, entry in sorted(spec['files'].items()):
            parts = Path(rel)
            if parts.is_absolute() or '..' in parts.parts or parts.parts[0] != 'src': raise ValueError('unsafe path')
            source = package / parts
            if sha(source) != entry['sha256']: raise ValueError('package digest mismatch: ' + rel)
            if rel in written: raise ValueError('duplicate owner: ' + rel)
            target = out / parts
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            written[rel] = name
    (out / 'assembly.json').write_text(json.dumps({'modules': sorted(selected), 'files': written}, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'modules': sorted(selected), 'files': len(written)}))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('split'); a.add_argument('--out', type=Path, required=True)
    a = sub.add_parser('compose'); a.add_argument('--packages', type=Path, required=True); a.add_argument('--out', type=Path, required=True); a.add_argument('modules', nargs='+', choices=NAMES)
    args = parser.parse_args()
    if args.cmd == 'split': split(args.out.resolve())
    else: compose(args.packages.resolve(), args.out.resolve(), args.modules)
