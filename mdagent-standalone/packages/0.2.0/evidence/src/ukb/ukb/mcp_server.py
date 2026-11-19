"""MCP stdio server for agents: read, contribute and prereview. Admin decisions are not exposed here.

Agents are expected to contribute: when their work produces reusable knowledge they summarise it and submit it
(ukb_guide -> ukb_prepare -> ukb_propose). Another agent prereviews; an administrator approves in the web
console or CLI.
"""
import json
import os
import sys
import traceback

from . import agent_view
from .api import UKB
from .core import navigate
from .core.errors import UKBError

PROTOCOL_VERSION_FALLBACK = "2024-11-05"
SCOPE_SCHEMA = {"type": "object", "description": "{facet: [tag, ...]}; same facet = OR, across facets = AND; "
                                                 "parent tags include children; labels and aliases accepted",
                "additionalProperties": {"type": "array", "items": {"type": ["string", "integer"]}}}
TAGS_SCHEMA = {"type": "array", "description": "[{facet, tag, reason?}] or [{facet, new_tag:{slug,label,description,"
                                               "parent?,aliases?}, reason}]", "items": {"type": "object"}}

INSTRUCTIONS = (
    "UKB is the shared knowledge base, disclosed level by level. Read before you act: ukb_overview (facets, then "
    "tags) -> ukb_catalog (titles in the scope you chose) -> ukb_read (only the few entries you picked). Contribute after you act: whenever your work produced something reusable (a fixed "
    "failure, a correction you received, a working setup, a finished case, a paper the user gave you), summarise it "
    "and submit it: ukb_guide (once) -> ukb_prepare (read what already exists) -> ukb_propose. When idle, take "
    "prereview work from ukb_prereview_queue. Nothing you submit is visible until another agent prereviews it and an "
    "administrator approves it.")


def _t(name, desc, props, required=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": list(required),
                            "additionalProperties": False}}


PROFILE = {"type": "string", "description": "consumer profile, default 'agent'"}
FORMAT = {"type": "string", "enum": ["text", "json"], "description": "text (default, compact lines) or json"}
TOOLS = [
    _t("ukb_must_read", "Read the core entries every task under this profile must read (full text).",
       {"profile": PROFILE}),
    _t("ukb_overview", "Level 1. Without facet: the facets and how many entries each covers. With facet: the top tags "
       "of that facet with descriptions, aliases, entry counts (including child tags) and 'children'; pass "
       "parent=<tag> to open its children. A parent tag in a scope already includes its children. Optional "
       "'within' restricts counts, e.g. {\"facet\": \"method\", \"within\": {\"topic\": [\"mechanical-loading\"]}}. "
       "Never returns entries.",
       {"profile": PROFILE, "facet": {"type": "string"}, "parent": {"type": "string"}, "within": SCOPE_SCHEMA,
        "describe": {"type": "boolean", "description": "include tag descriptions and aliases"}, "format": FORMAT}),
    _t("ukb_catalog", "Level 2. Titles of the entries in a scope (ref, type, title, tags); no content. At most "
       "about 20 titles: if the scope is larger the reply says so and lists tags (with counts) to narrow by; there is "
       "no paging. Only refs listed here can be read with ukb_read; pass them as given (entry_id@rev or entry_id).",
       {"profile": PROFILE, "scope": SCOPE_SCHEMA, "types": {"type": "array", "items": {"type": "string"}},
        "format": FORMAT}, ("scope",)),
    _t("ukb_read", "Level 3. Content of entries you picked from a ukb_catalog result in this session (at most 3 per "
       "call; use the #handles). Cases and references first show their core fields; depth=full adds reuse and data "
       "details. Cases and references return their reviewed summary and file list; use ukb_read_original "
       "for originals.",
       {"profile": PROFILE,
        "entry_refs": {"type": "array", "items": {"type": "string"},
                       "description": "handles like #3 or refs from ukb_catalog"},
        "view": {"type": "string", "enum": ["compact", "full"]},
        "depth": {"type": "string", "enum": ["core", "full"],
                  "description": "core (default): enough to judge and answer; full: also reuse/setup details of "
                                 "cases and data tables of references"},
        "format": FORMAT}, ("entry_refs",)),
    _t("ukb_read_original", "Read an original file of a document entry, whole or by section id, page (p3 / p3-5) or "
       "lines (L10-40); long selections are paged with cursor.",
       {"profile": PROFILE, "ref": {"type": "string", "description": "#handle or entry ref"},
        "path": {"type": "string"}, "section": {"type": "string"}, "page": {"type": "string"},
        "lines": {"type": "string"}, "cursor": {"type": "string"}, "format": FORMAT}, ("ref", "path")),
    _t("ukb_checkout", "Copy reusable files of a case (or chosen paths) into a workspace directory, with a "
       "UKB_PROVENANCE json listing source, hashes and the parameters that must be adapted. Never executes files.",
       {"profile": PROFILE, "ref": {"type": "string", "description": "#handle or entry ref"},
        "dest": {"type": "string"},
        "paths": {"type": "array", "items": {"type": "string"}}, "overwrite": {"type": "boolean"},
        "task_ref": {"type": "string"}}, ("ref", "dest")),

    _t("ukb_guide", "How to contribute: when and what to submit, the workflow, payload examples and the list of types "
       "and facets. Pass type_id for that type's field spec; tags come from ukb_overview.",
       {"type_id": {"type": "string"}}),
    _t("ukb_prepare", "Before submitting: give the type and tags you intend to use. Validates the tags and returns the "
       "titles of entries already in those tags (and open proposals there); read the ones that look similar with "
       "ukb_read, then decide new / revise / reinforce / skip.",
       {"profile": PROFILE, "type_id": {"type": "string"}, "tags": TAGS_SCHEMA}, ("type_id", "tags")),
    _t("ukb_propose", "Submit knowledge you summarised, wrapped as {\"payload\": {...}}. See ukb_guide.payload_examples "
       "for exact shapes of new / revise / reinforce. Becomes visible only after AI prereview and admin approval.",
       {"payload": {"type": "object"}}, ("payload",)),
    _t("ukb_propose_tag", "Propose adding/removing tags on an existing entry (admin approves).",
       {"entry": {"type": "string"}, "add": TAGS_SCHEMA, "drop": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"}}, ("entry", "reason")),
    _t("ukb_propose_status", "Propose a status change (deprecated / revoked / superseded / active) with reason.",
       {"entry": {"type": "string"}, "target_status": {"type": "string"}, "reason": {"type": "string"},
        "by_entry": {"type": "string"}}, ("entry", "target_status", "reason")),
    _t("ukb_feedback", "Report how an entry worked out (helpful, neutral, misleading, not_applicable, reused_ok, "
       "reused_failed). Negative reports open a recheck for the admin.",
       {"entry": {"type": "string"}, "outcome": {"type": "string"}, "note": {"type": "string"},
        "task_ref": {"type": "string"}}, ("entry", "outcome")),
    _t("ukb_my_submissions", "Your own submissions with their state and the prereview/admin notes; follow up on "
       "changes_requested.",
       {"state": {"type": "string"}, "limit": {"type": "integer"}}),

    _t("ukb_prereview_queue", "Proposals from other agents that still need AI prereview.",
       {"limit": {"type": "integer"}}),
    _t("ukb_prereview_bundle", "AI prereview: fetch one page of the material for a task (pending entry, origin, "
       "originals, taxonomy with tag descriptions, and every entry in the same tag scope). Read all pages, then "
       "submit. You cannot prereview your own submission.",
       {"task_id": {"type": "string"}, "page": {"type": "integer"}}, ("task_id",)),
    _t("ukb_prereview_submit", "AI prereview: submit the structured opinion (type, tags, duplicates/conflicts, "
       "checklist, summary fidelity, recommendation) with all page tokens. Advisory only.",
       {"task_id": {"type": "string"}, "page_tokens": {"type": "array", "items": {"type": "string"}},
        "opinion": {"type": "object"}}, ("task_id", "page_tokens", "opinion")),
]


TOOL_INDEX = {t["name"]: t for t in TOOLS}


class Server:
    def __init__(self, kb, agent_id, seen=None, handles=None):
        self.kb = kb
        self.agent = agent_id
        self.seen = seen if seen is not None else set()
        self.handles = handles if handles is not None else agent_view.Handles()

    def _ref(self, ref):
        eid = self.handles.resolve(ref)
        if eid is None:
            raise UKBError("ENTRY_REF_INVALID", "unknown handle", ref=ref)
        if eid not in self.seen:
            raise UKBError("NOT_LISTED", "open the entry's catalog first", ref=ref)
        return eid

    def call(self, name, a):
        spec = TOOL_INDEX.get(name)
        if spec is None:
            raise UKBError("TOOL_UNKNOWN", "unknown tool", name=name, tools=sorted(TOOL_INDEX))
        schema = spec["inputSchema"]
        if not isinstance(a, dict):
            raise UKBError("ARGUMENT_INVALID", "arguments must be an object")
        unknown = sorted(set(a) - set(schema["properties"]))
        if unknown:
            raise UKBError("ARGUMENT_UNKNOWN", "unknown argument(s); nothing was done",
                           unknown=unknown, accepted=sorted(schema["properties"]))
        missing = [k for k in schema.get("required", []) if a.get(k) in (None, "")]
        if missing:
            raise UKBError("ARGUMENT_MISSING", "required argument(s) missing", missing=missing,
                           accepted=sorted(schema["properties"]))
        kb = self.kb
        p = a.get("profile") or "agent"
        if name == "ukb_must_read":
            return kb.must_read(p)
        text = a.get("format", "text") == "text"
        if name == "ukb_overview":
            out = kb.overview(p, a.get("facet"), a.get("within"), a.get("parent"))
            return agent_view.overview_text(out, bool(a.get("describe"))) if text else out
        if name == "ukb_catalog":
            out = kb.catalog(p, a.get("scope"), a.get("types"))
            self.seen.update(i["ref"].split("@")[0] for i in out["items"])
            return agent_view.catalog_text(out, self.handles, a.get("scope")) if text else out
        if name == "ukb_read":
            if not isinstance(a["entry_refs"], list):
                raise UKBError("ARGUMENT_INVALID", "entry_refs must be a list")
            ids = []
            for r in a["entry_refs"]:
                eid = self.handles.resolve(r)
                if eid is None:
                    raise UKBError("ENTRY_REF_INVALID", "unknown handle", ref=r)
                ids.append(navigate.parse_ref(eid)[0])
            unseen = [r for r in ids if r not in self.seen]
            if unseen:
                raise UKBError("NOT_LISTED", "read only entries listed by ukb_catalog in this session; look at the "
                               "titles first", refs=unseen)
            out = kb.read(p, None, ids, None, None, a.get("view", "compact"))
            return agent_view.read_text(out, self.handles, a.get("depth", "core")) if text else out
        if name == "ukb_read_original":
            out = kb.read_original(p, self._ref(a["ref"]), a["path"], a.get("section"), a.get("page"),
                                   a.get("lines"), a.get("cursor"))
            if text:
                head = "%s %s%s" % (out["path"], out.get("selection", ""),
                                    "" if out.get("readable") else "（无可读文本）")
                body = out.get("text") or out.get("note", "")
                more = "\n（未完，cursor=%s）" % out["next_cursor"] if out.get("next_cursor") else ""
                secs = ""
                if out.get("sections") and not (a.get("section") or a.get("page") or a.get("lines")):
                    secs = "\n章节: " + "；".join("%s %s" % (s["section_id"], s["title"]) for s in out["sections"])
                return head + secs + "\n" + body + more
            return out
        if name == "ukb_checkout":
            return kb.checkout(p, self._ref(a["ref"]), a["dest"], self.agent, a.get("paths"), False,
                               bool(a.get("overwrite")), a.get("task_ref"))
        if name == "ukb_guide":
            return kb.guide(a.get("type_id"))
        if name == "ukb_prepare":
            out = kb.prepare(p, a["type_id"], a["tags"])
            self.seen.update(i["ref"].split("@")[0] for i in out["existing"])
            for i in out["existing"]:
                i["handle"] = "#%d" % self.handles.get(i["ref"].split("@")[0])
            return out
        if name == "ukb_propose":
            return kb.propose(a["payload"], "mcp", self.agent)
        if name == "ukb_propose_tag":
            return kb.propose_tags(a["entry"], a.get("add"), a.get("drop"), a["reason"], self.agent)
        if name == "ukb_propose_status":
            return kb.propose_status(a["entry"], a["target_status"], a["reason"], self.agent, a.get("by_entry"))
        if name == "ukb_feedback":
            return kb.feedback(a["entry"], self.agent, a["outcome"], a.get("note"), a.get("task_ref"))
        if name == "ukb_my_submissions":
            return kb.my_submissions(self.agent, a.get("state"), int(a.get("limit", 50)))
        if name == "ukb_prereview_queue":
            mine = self.agent.split(":", 1)[-1]
            tasks = [t for t in kb.queue("open", limit=int(a.get("limit", 50)) * 2, needs="prereview")
                     if t["submitter"].split(":", 1)[-1] != mine]
            return {"tasks": tasks[:int(a.get("limit", 50))]}
        if name == "ukb_prereview_bundle":
            return kb.prereview_bundle(a["task_id"], int(a.get("page", 0)))
        if name == "ukb_prereview_submit":
            return kb.prereview_submit(a["task_id"], "llm:" + self.agent.split(":", 1)[-1], a["page_tokens"],
                                       a["opinion"])
        raise UKBError("TOOL_UNKNOWN", "unknown tool", name=name)

    def handle(self, msg):
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION_FALLBACK
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": pv, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ukb", "version": "0.2.0"}, "instructions": INSTRUCTIONS}}
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                data = self.call(name, args)
                if isinstance(data, str):
                    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": data}]}}
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}],
                    "structuredContent": data if isinstance(data, dict) else {"result": data}}}
            except UKBError as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(e.to_dict(), ensure_ascii=False)}],
                    "isError": True}}
            except KeyError as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "error": "ARGUMENT_MISSING",
                                                                      "message": str(e)})}], "isError": True}}
            except Exception as e:
                sys.stderr.write(traceback.format_exc())
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "error": "INTERNAL",
                                                                      "message": str(e)})}], "isError": True}}
        if mid is not None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
        return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="ukb-mcp")
    ap.add_argument("--root", default=os.environ.get("UKB_ROOT"))
    ap.add_argument("--agent", default=os.environ.get("UKB_AGENT_ID", "agent"))
    a = ap.parse_args(argv)
    if not a.root:
        sys.stderr.write("UKB root required (--root or UKB_ROOT)\n")
        return 2
    srv = Server(UKB(a.root), "agent:" + a.agent)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        out = srv.handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
