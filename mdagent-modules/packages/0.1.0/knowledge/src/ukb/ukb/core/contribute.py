"""Support for AI agents that summarise their own work and submit it.

Three read helpers, no writes:
- guide(): what can be submitted and how (types with field specs, facets and tag descriptions, file rules, the
  origin contract). Lets an agent build a valid proposal in one go instead of guessing.
- prepare(): given the type and tags the agent intends to use, return the full text of what is already in that
  scope plus open proposals in it, so the agent itself can decide new / revise / reinforce / skip before
  submitting. The code does not judge similarity.
- mine(): the agent's own submissions with their outcome and the admin's/prereviewer's notes, so it can follow up
  on changes_requested and learn from rejections.
"""
import json

from . import navigate, registry
from .errors import UKBError
from .intake import CONTEXT_KINDS, resolve_tag_proposals

WHEN_TO_SUBMIT = [
    "You found the cause of a failure and how to fix or avoid it (lesson / failure_pattern).",
    "A user or reviewer corrected you, and the correction is reusable beyond this task (correction / lesson).",
    "You finished a task whose inputs and results others could reuse (case, with the reusable files).",
    "You read a paper or document the user supplied and it contains values or methods worth reusing (reference).",
    "You confirmed a value, setting or procedure that worked and could be stated generally (parameter / procedure / "
    "rule).",
    "An existing entry turned out wrong or incomplete (revise, or ukb_feedback misleading).",
    "An existing entry was confirmed again by independent evidence (reinforce).",
]
DO_NOT_SUBMIT = [
    "Things specific to one task that nobody would reuse.",
    "Unverified guesses: state them as claim/observation with sources, or do not submit.",
    "Copies of what is already in the knowledge base: read the scope with ukb_prepare first.",
    "Large raw outputs and archives; the knowledge base refuses them (see file_rules.denied).",
    "Secrets, credentials, personal data.",
]


PAYLOAD_EXAMPLES = {
    "call": "ukb_propose arguments are {\"payload\": <one of the objects below>}; ukb_prepare takes "
            "{\"type_id\": ..., \"tags\": [{\"facet\": ..., \"tag\": ...}]}",
    "new": {"mode": "new", "type_id": "lesson", "title": "<one line>",
            "fields": {"<field from types[].fields>": "<value>"},
            "tags": [{"facet": "topic", "tag": "<slug, label or alias>"},
                     {"facet": "topic", "new_tag": {"slug": "<a-z0-9-_.>", "label": "<name>",
                                                    "description": "<what it includes / excludes>",
                                                    "parent": "<optional parent tag>", "aliases": ["<optional>"]},
                      "reason": "<why no existing tag fits>"}],
            "conditions": [{"key": "<condition_keys[].key_id>", "op": "eq|ne|lt|le|gt|ge|in|range|exists",
                            "value": "<typed value>", "note": "<optional>"}],
            "sources": [{"source_type": "<source_types[].source_type_id>", "title": "<title>",
                         "locator": "<path, doi, url>", "role": "supports|contradicts|derived_from|example",
                         "locator_detail": "<optional line/page>", "excerpt": "<optional>"}],
            "origin": {"kind": "task", "ref": "<task or run id>", "summary_of_evidence": "<what you observed>"}},
    "document_files": [{"path": "in.main", "role": "<file_rules.roles[].role_id>", "description": "<what it is>",
                        "source_path": "<absolute path inside an intake root>"},
                       {"path": "notes.md", "role": "report", "description": "<what it is>",
                        "content_text": "<inline text instead of source_path>"}],
    "revise": {"mode": "revise", "target_entry_id": "<entry id>", "base_rev_no": "<rev_no you read>",
               "title": "...", "fields": {}, "tags": [], "sources": [], "origin": {},
               "note_on_files": "for documents, list files again; keep unchanged ones with {\"path\": ..., \"keep\": true}"},
    "reinforce": {"mode": "reinforce", "target_entry_id": "<entry id>",
                  "note": "<what the new, independent evidence confirms>",
                  "sources": [{"source_type": "...", "title": "...", "locator": "..."}], "origin": {}},
}


def guide(conn, type_id=None):
    if type_id is not None:
        s = registry.type_spec(conn, type_id)
        spec = s["spec"]
        out = {"type_id": s["type_id"], "label": s["label"], "description": s["description"],
               "tier": spec.get("tier", "entry"), "fields": spec["fields"],
               "required_facets": spec.get("required_facets", []), "require_sources": spec.get("require_sources", True),
               "review_checklist": spec.get("review_checklist", [])}
        if spec.get("tier") == "document":
            out["document"] = spec["document"]
            out["file_rules"] = _file_rules(conn)
        return out
    types = [{"type_id": r["type_id"], "label": r["label"], "description": r["description"]}
             for r in conn.execute("SELECT * FROM reg_type WHERE status='active' ORDER BY type_id")]
    facets = [{"facet": f["facet_id"], "label": f["label"], "description": f["description"]}
              for f in conn.execute("SELECT * FROM reg_facet WHERE status='active' ORDER BY ord, facet_id")]
    return {
        "when_to_submit": WHEN_TO_SUBMIT,
        "do_not_submit": DO_NOT_SUBMIT,
        "workflow": [
            "1. Decide the type from 'types' by what the knowledge is; call ukb_guide(type_id) for its fields.",
            "2. Pick tags with ukb_overview (facet, then parent=<tag> to go down), reading tag descriptions; prefer "
            "the most specific fitting tag; use a parent only if no child fits. Propose a new tag only when no existing tag or alias covers it, with a "
            "reason and a description that says what it includes and excludes.",
            "3. Call ukb_prepare with that type and tags; read the similar-looking titles with ukb_read; then "
            "choose: new, revise (same entry, better content), reinforce (same knowledge, new independent evidence) "
            "or skip.",
            "4. Write fields in your own words, self-contained, one point per entry; put applicability limits in "
            "conditions or the text; cite sources you actually used.",
            "5. Fill origin {kind, ref, summary_of_evidence}: where this came from and why you believe it.",
            "6. Submit with ukb_propose. Check ukb_my_submissions later and address changes_requested.",
        ],
        "origin": {"kinds": list(CONTEXT_KINDS),
                   "fields": {"kind": "required", "ref": "required: task id, conversation id, run dir, file name",
                              "summary_of_evidence": "required: what you observed that supports this",
                              "excerpt": "optional short quote", "task": "optional task description"}},
        "locators": "[[path]] [[path#s2]] [[path#p3]] [[path#p3-5]] [[path:L10-20]]; path is the file's 'path' in "
                    "this payload, not the source file name",
        "payload_examples": PAYLOAD_EXAMPLES,
        "types": types,
        "facets": facets,
        "source_types": [dict(r) for r in conn.execute("SELECT source_type_id, label FROM reg_source_type WHERE "
                                                       "status='active' ORDER BY source_type_id")],
        "condition_keys": [dict(r) for r in conn.execute("SELECT key_id, label, value_type, unit, enum_json FROM "
                                                         "reg_condition_key WHERE status='active' ORDER BY key_id")],
        "daily_limit": registry.setting(conn, "agent_daily_proposal_limit"),
    }


def _file_rules(conn):
    return {"allowed_formats": [{"format": r["format_id"], "patterns": json.loads(r["patterns_json"]),
                                 "max_bytes": r["max_bytes"]}
                                for r in conn.execute("SELECT * FROM reg_file_format WHERE status='active' "
                                                      "ORDER BY format_id")],
            "denied": [r["pattern"] for r in conn.execute("SELECT pattern FROM reg_file_deny WHERE status='active' "
                                                          "ORDER BY pattern")],
            "max_file_bytes": registry.setting(conn, "max_file_bytes"),
            "roles": [dict(r) for r in conn.execute("SELECT role_id, label, description, default_reusable FROM "
                                                    "reg_artifact_role WHERE status='active' ORDER BY role_id")]}


def prepare(conn, prof, type_id, tags):
    """Validate the intended tags; return titles of entries already in that scope, plus open proposals there."""
    tspec = registry.type_spec(conn, type_id)
    props = resolve_tag_proposals(conn, tags, tspec["spec"].get("required_facets", []))
    scope = {}
    new_tags = []
    for p in props:
        if p["tag_id"] is None:
            new_tags.append("%s/%s" % (p["facet"], p["new_tag"]["slug"]))
            parent = p["new_tag"]["parent_tag_id"]
            if parent:
                scope.setdefault(p["facet"], []).append(parent)
            continue
        scope.setdefault(p["facet"], []).append(p["tag_id"])
    # Titles only. Union per facet, then rank by how many of the intended tags an entry shares.
    want = set()
    for facet, ids in scope.items():
        for i in ids:
            want.update(registry.tag_descendants(conn, i))
    hits = {}
    for facet, ids in scope.items():
        for row in navigate.catalog(conn, prof, {facet: ids}, limit=10000)["items"]:
            hits.setdefault(row["ref"], row)
    def overlap(row):
        eid = row["ref"].split("@")[0]
        return len({r[0] for r in conn.execute("SELECT tag_id FROM entry_tag WHERE entry_id=? AND state='confirmed'",
                                               (eid,))} & want)
    items = sorted(hits.values(), key=lambda r: (-overlap(r), r["title"]))
    for r in items:
        r["shared_tags"] = overlap(r)
    cap = prof.get("catalog_limit", 30)
    shown = items[:cap]
    pending = []
    tag_ids = {i for ids in scope.values() for i in ids}
    expanded = set()
    for i in tag_ids:
        expanded.update(registry.tag_descendants(conn, i))
    if expanded:
        q = ("SELECT DISTINCT t.task_id, t.kind, t.submitter, r.title, e.entry_id FROM review_task t JOIN entry e ON "
             "e.entry_id=t.entry_id JOIN entry_revision r ON r.rev_id=t.rev_id JOIN entry_tag et ON "
             "et.entry_id=e.entry_id WHERE t.state='open' AND t.kind IN ('new','revise') AND et.tag_id IN (%s) "
             "ORDER BY t.opened_at" % ",".join("?" * len(expanded)))
        pending = [dict(r) for r in conn.execute(q, list(expanded))]
    return {"type": type_id, "scope": {f: [_slug(conn, i) for i in ids] for f, ids in scope.items()},
            "new_tags_proposed": new_tags, "existing_count": len(items), "existing": shown,
            "open_proposals_in_scope": [{"task_id": p["task_id"], "entry_id": p["entry_id"], "title": p["title"]}
                                        for p in pending],
            "decide": "Look at the titles (most shared tags first). ukb_read the few that might say the same thing. "
                      "If one already states this knowledge: skip, or reinforce it with new independent evidence, or "
                      "revise it (target_entry_id + base_rev_no). Otherwise propose a new entry."}


def _slug(conn, tag_id):
    r = conn.execute("SELECT slug FROM tag WHERE tag_id=?", (tag_id,)).fetchone()
    return r[0] if r else tag_id


def mine(conn, submitter, state=None, limit=50):
    if not isinstance(submitter, str) or not submitter:
        raise UKBError("PAYLOAD_INVALID", "submitter is required")
    q = "SELECT * FROM intake WHERE submitter=?"
    args = [submitter]
    if state:
        q += " AND state=?"
        args.append(state)
    q += " ORDER BY received_at DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for i in conn.execute(q, args):
        item = {"intake_id": i["intake_id"], "received_at": i["received_at"], "mode": i["mode"], "state": i["state"],
                "entry_id": i["result_entry_id"] or i["target_entry_id"]}
        if i["issues_json"]:
            item["refused"] = json.loads(i["issues_json"])
        t = conn.execute("SELECT * FROM review_task WHERE intake_id=?", (i["intake_id"],)).fetchone()
        if t is not None:
            item["task_id"] = t["task_id"]
            item["task_state"] = t["state"]
            r = conn.execute("SELECT title FROM entry_revision WHERE rev_id=?", (t["rev_id"],)).fetchone() \
                if t["rev_id"] else None
            item["title"] = r[0] if r else None
            item["feedback"] = [{"by": d["reviewer_id"], "kind": d["reviewer_kind"], "decision": d["decision"],
                                 "note": d["rationale"]}
                                for d in conn.execute("SELECT * FROM review_decision WHERE task_id=? ORDER BY "
                                                      "decided_at", (t["task_id"],))]
            if t["state"] == "changes_requested":
                item["next"] = "fix the points in feedback and submit again with mode=revise and target_entry_id"
        out.append(item)
    return {"submitter": submitter, "count": len(out), "items": out}
