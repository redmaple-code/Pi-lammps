"""Verify a legacy import: every old id must resolve through the compatibility layer, with the same status.

  python tools/kb/verify_import.py [--data-root DIR] [--atoms PATH]
Exit 0 when there are no missing ids and no status mismatches.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402
from tools.kb import import_legacy  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root")
    ap.add_argument("--atoms")
    a = ap.parse_args()
    os.environ["MDAGENT_KB"] = "ukb"
    pairs = []
    for path, pack, entry, _consumer in import_legacy._packs():
        pairs.append(("review_experience", pack, entry.get("id"), entry.get("status") or "active",
                      entry.get("kind") or ""))
    atoms_path = a.atoms or os.path.join(adapter.data_root(a.data_root), "memory", "atoms.jsonl")
    for atom in import_legacy._atom_rows(atoms_path):
        status = "revoked" if atom.get("retracted") else ("superseded" if atom.get("superseded_by") else "active")
        pairs.append(("atoms", "atoms.jsonl", atom.get("id"), status, atom.get("kind")))
    handle = adapter.kb(a.data_root)
    rows = handle.conn.execute("SELECT DISTINCT c.value_json, e.status FROM entry_condition c JOIN entry_revision r ON r.rev_id=c.rev_id JOIN entry e ON e.entry_id=r.entry_id WHERE c.key_id='legacy_id'").fetchall()
    imported_ids = {json.loads(r[0]): (r[1] if r[1] != 'trial' else 'canary') for r in rows}
    idx = adapter.exp_status_index(a.data_root)
    dims = adapter.load_dimensions(a.data_root)
    surf = adapter.load_items(a.data_root)
    anchors = adapter.load_anchors(a.data_root)
    mapping = {"physics-anchor": ("anchors", lambda i: i in anchors),
               "methodology-dimension": ("dimensions", lambda i: any(e["id"] == i for v in dims.values() for e in v)),
               None: ("surface", lambda i: i in surf)}
    missing, mismatched = [], []
    for source, pack, eid, status, kind in pairs:
        if str(eid) not in imported_ids:
            missing.append({"source": source, "pack": pack, "id": eid})
            continue
        ukb_status = imported_ids[str(eid)]
        if status in ("active", "canary", "revoked", "superseded") and ukb_status not in (status, "candidate"):
            mismatched.append({"id": eid, "legacy": status, "ukb": ukb_status})
    report = {"store": adapter.ukb_root(a.data_root), "legacy_entries": len(pairs),
              "indexed": len(idx), "anchors": len(anchors), "surface": len(surf),
              "dimension_families": len(dims), "missing": missing[:50], "missing_total": len(missing),
              "status_mismatch": mismatched[:50], "status_mismatch_total": len(mismatched),
              "ok": not missing and not mismatched}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
