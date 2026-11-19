"""Import the legacy MDagent knowledge base into UKB as candidate entries (nothing is approved here).

Sources, all read-only:
  <repo>/mcp/review_experience/*.json        review_experience packs (5 schemas)
  <data_root>/memory/atoms.jsonl             legacy atoms

  python tools/kb/import_legacy.py --dry-run
  python tools/kb/import_legacy.py --atoms <atoms.jsonl>      # e.g. rsynced from the server

Every entry keeps its original record in ``raw_json`` and its old id in the ``legacy_id`` condition, so the
compatibility layer can hand consumers exactly what they saw before. Re-running skips ids already imported.
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402

PACK_DIR = os.path.join(REPO, "mcp", "review_experience")
KIND_MAP = os.path.join(REPO, "data", "domain", "mdagent_legacy", "kind_map.json")


def _map():
    with open(KIND_MAP, encoding="utf-8") as f:
        return json.load(f)


def _existing(handle):
    rows = handle.conn.execute(
        "SELECT DISTINCT c.value_json FROM entry_condition c JOIN entry_revision r ON r.rev_id=c.rev_id "
        "JOIN entry e ON e.entry_id=r.entry_id WHERE c.key_id='legacy_id' AND e.status<>'rejected'").fetchall()
    out = set()
    for (v,) in rows:
        try:
            out.add(json.loads(v))
        except ValueError:
            pass
    return out


def _packs():
    for name in sorted(os.listdir(PACK_DIR)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        path = os.path.join(PACK_DIR, name)
        with open(path, encoding="utf-8") as f:
            pack = json.load(f)
        pack_name = pack.get("pack") or os.path.splitext(name)[0]
        pack_consumer = pack.get("consumer")
        entries = pack.get("entries")
        if entries is None:
            # Pack-level catalogues carry no refs; the legacy recall drops them as invalid, so skip them here too.
            continue
        for i, e in enumerate(entries):
            if isinstance(e, dict) and e.get("id"):
                yield path, pack_name, e, (pack_consumer if pack_consumer else None)


def _atom_rows(path):
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("id"):
                yield row


def _payload_for_entry(mapping, pack, entry, consumer=None):
    if consumer:
        entry = dict(entry, _pack_consumer=consumer)
    kind = str(entry.get("kind") or "")
    type_id = mapping["kinds"].get(kind) or mapping["packs"].get(pack) or mapping["default"]
    raw = json.dumps(entry, ensure_ascii=False)[:20000]
    statement = str(entry.get("question") or entry.get("statement") or entry.get("quantity") or entry.get("id"))
    fields = {"statement": statement[:3900], "raw_json": raw}
    if type_id == "legacy_anchor":
        fields.update({"quantity": str(entry.get("quantity") or "")[:480], "unit": str(entry.get("unit") or "")[:110],
                       "rationale": str(entry.get("rationale") or "")[:3900]})
    elif type_id in ("legacy_methodology", "legacy_dimension", "legacy_entry"):
        sev = entry.get("severity_hint") or entry.get("severity")
        fields["severity"] = sev if sev in ("hard", "soft", "info") else "soft"
        fields["rationale"] = str(entry.get("rationale") or "")[:3900]
        if isinstance(entry.get("remediation"), list):
            fields["remediation"] = [str(x)[:400] for x in entry["remediation"]][:20]
    if type_id == "legacy_methodology":
        fields["statement"] = statement[:1900]
        fields["raw_json"] = raw
    elif type_id == "legacy_dimension":
        fields["family"] = str(entry.get("family") or "")[:190]
    elif type_id == "legacy_surface":
        fields["phase"] = str(entry.get("phase") or "")[:70]
        fields["domain"] = str(entry.get("domain") or "")[:190]
    elif type_id == "legacy_entry":
        fields["kind"] = kind[:110]
    if type_id == "legacy_dimension":
        fields["statement"] = statement[:1900]
    conditions = [{"key": "legacy_id", "value": str(entry["id"])},
                  {"key": "legacy_pack", "value": str(pack)}]
    if kind:
        conditions.append({"key": "legacy_kind", "value": kind})
    consumer = str((entry.get("_pack_consumer") or ""))
    if consumer:
        conditions.append({"key": "legacy_consumer", "value": consumer})
    if entry.get("family"):
        conditions.append({"key": "legacy_family", "value": str(entry["family"])})
    sources = []
    for ref in (entry.get("refs") or entry.get("source_refs") or [])[:8]:
        sources.append({"source_type": "legacy_ref", "title": str(ref).split(":")[0][:80], "locator": str(ref)[:480]})
    return {"mode": "new", "type_id": type_id,
            "title": ("[%s] %s" % (kind or pack, statement))[:120],
            "fields": fields, "tags": [{"facet": "topic", "tag": "legacy"}],
            "conditions": conditions, "sources": sources,
            "origin": {"kind": "document", "ref": "legacy:%s/%s" % (pack, entry["id"]),
                       "summary_of_evidence": "imported from MDagent legacy knowledge base (%s)" % pack}}


def _payload_for_atom(mapping, atom):
    raw = json.dumps(atom, ensure_ascii=False)[:20000]
    refs = [str(r)[:480] for r in (atom.get("refs") or [])][:8]
    sources = [{"source_type": "legacy_ref", "title": str(r).split(":")[0][:80], "locator": r} for r in refs]
    return {"mode": "new", "type_id": "legacy_atom",
            "title": ("[atom] %s" % str(atom.get("fact") or atom["id"]))[:120],
            "fields": {"statement": str(atom.get("fact") or "")[:1900], "kind": str(atom.get("kind") or "")[:110],
                       "refs": refs, "raw_json": raw},
            "tags": [{"facet": "topic", "tag": "legacy"}],
            "conditions": [{"key": "legacy_id", "value": str(atom["id"])},
                           {"key": "legacy_pack", "value": "atoms.jsonl"}]
            + ([{"key": "legacy_kind", "value": str(atom["kind"])}] if atom.get("kind") else []),
            "sources": sources,
            "origin": {"kind": "document", "ref": "legacy:atoms/" + str(atom["id"]),
                       "summary_of_evidence": "imported from legacy atoms.jsonl"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data-root")
    ap.add_argument("--atoms", help="path to legacy atoms.jsonl (e.g. rsynced from the server)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    mapping = _map()
    handle = adapter.kb(a.data_root)
    taken = _existing(handle)
    plan, skipped = [], []
    for path, pack, entry, pack_consumer in _packs():
        if str(entry["id"]) in taken:
            skipped.append(("review_experience", pack, entry["id"]))
            continue
        plan.append(("review_experience", pack,
                     _payload_for_entry(mapping, pack, entry, pack_consumer)))
    atoms_path = a.atoms or os.path.join(adapter.data_root(a.data_root), "memory", "atoms.jsonl")
    n_atoms = 0
    for atom in _atom_rows(atoms_path):
        n_atoms += 1
        if str(atom["id"]) in taken:
            skipped.append(("atoms", "atoms.jsonl", atom["id"]))
            continue
        plan.append(("atoms", "atoms.jsonl", _payload_for_atom(mapping, atom)))
    if a.limit:
        plan = plan[:a.limit]
    report = {"store": adapter.ukb_root(a.data_root), "atoms_source": atoms_path if os.path.exists(atoms_path) else None,
              "atoms_seen": n_atoms, "planned": len(plan), "skipped_existing": len(skipped),
              "by_type": {}, "errors": []}
    if a.dry_run:
        for kind, pack, payload in plan:
            report["by_type"][payload["type_id"]] = report["by_type"].get(payload["type_id"], 0) + 1
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    created = 0
    for kind, pack, payload in plan:
        try:
            handle.propose(payload, "import", "import:legacy")
            created += 1
            report["by_type"][payload["type_id"]] = report["by_type"].get(payload["type_id"], 0) + 1
        except Exception as exc:
            report["errors"].append({"pack": pack, "id": payload["origin"]["ref"], "error": str(exc)[:200]})
    report["created"] = created
    report["next"] = "review in the console: python tools/kb/serve.py"
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
