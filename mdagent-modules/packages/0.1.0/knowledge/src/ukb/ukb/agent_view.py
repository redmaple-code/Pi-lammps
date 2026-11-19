"""Token-lean text rendering of the staged read levels for agents (MCP only).

The structured API (ukb.api / navigate) is unchanged. Agents get plain lines instead of indented JSON:
- overview: one line per tag "slug 名称 (n) [+k子]" and, only on request, descriptions;
- catalog: one line per entry "#n 类型 标题", tags shared with the chosen scope are not repeated;
- read: entry content as compact "field: value" lines, no ids/hashes/repeated metadata.
Short handles #n are local to the MCP session and map to entry ids.
"""
import json

TYPE_ABBR = {"lesson": "经验", "rule": "规则", "parameter": "参数", "procedure": "流程", "failure_pattern": "故障",
             "case": "案例", "reference": "文献", "document": "文档", "definition": "定义", "faq": "问答",
             "checklist": "清单", "correction": "纠正", "template": "模板", "claim": "断言", "observation": "观察"}


def _t(type_id):
    return TYPE_ABBR.get(type_id, type_id)


def overview_text(ov, with_desc=False):
    if "facets" in ov:
        lines = ["分类面（条目数）：" + "；".join("%s %s(%d)" % (f["facet"], f["label"], f["entries"])
                                          for f in ov["facets"] if f["entries"])]
        empty = [f["facet"] for f in ov["facets"] if not f["entries"]]
        if empty:
            lines.append("无条目：" + "、".join(empty))
        lines.append("下一步：overview(facet) 看该面标签")
        return "\n".join(lines)
    head = "%s 的%s标签（条目数，+k=有k个子标签）：" % (ov["facet"], "「%s」子" % ov["parent"] if ov.get("parent") else "顶层")
    rows = []
    for t in ov["tags"]:
        s = "%s %s(%d)" % (t["tag"], t["label"], t["entries"])
        if t.get("children"):
            s += "+%d" % t["children"]
        if with_desc:
            al = "，".join(a for a in t.get("aliases", [])[:4])
            s += " —— " + t["description"] + ("［别名：%s］" % al if al else "")
        rows.append(s)
    sep = "\n" if with_desc else "；"
    tail = "\n下一步：catalog(scope) 看标题；parent=<标签> 展开子标签" + ("" if with_desc else "；describe=true 看标签说明")
    return head + ("\n" if with_desc else "") + sep.join(rows) + tail


class Handles:
    """Session-local short numbers for entries (#1, #2 ...)."""

    def __init__(self):
        self.by_id = {}
        self.ids = []

    def get(self, entry_id):
        if entry_id not in self.by_id:
            self.ids.append(entry_id)
            self.by_id[entry_id] = len(self.ids)
        return self.by_id[entry_id]

    def resolve(self, ref):
        s = str(ref).strip()
        if s.startswith("#") and s[1:].isdigit():
            n = int(s[1:])
            if 1 <= n <= len(self.ids):
                return self.ids[n - 1]
            return None
        return s.split("@")[0]


def catalog_text(cat, handles, scope):
    scope_tags = set()
    for f, refs in (scope or {}).items():
        for r in (refs if isinstance(refs, list) else [refs]):
            scope_tags.add("%s/%s" % (f, r))
    lines = ["%d 条%s：" % (cat["count"], "（只显示前 %d）" % cat["shown"] if cat["shown"] < cat["count"] else "")]
    common = None
    for it in cat["items"]:
        tags = set(it["tags"])
        common = tags if common is None else common & tags
    for it in cat["items"]:
        n = handles.get(it["ref"].split("@")[0])
        extra = [t.split("/", 1)[1] for t in it["tags"] if t not in (common or set()) and t not in scope_tags]
        s = "#%d %s %s" % (n, _t(it["type"]), it["title"])
        if it.get("has_files"):
            s += "［附原文］"
        if it.get("status"):
            s += "［%s］" % it["status"]
        if extra:
            s += " ·" + ",".join(extra[:4])
        lines.append(s)
    if cat.get("narrow_by"):
        lines.append("范围太大，可加一个标签缩小：" + "；".join("%s/%s(%d)" % (h["facet"], h["tag"], h["entries"])
                                                  for h in cat["narrow_by"][:10]))
    lines.append("下一步：read 选中的 #编号（每次至多 3 条）")
    return "\n".join(lines)


def _val(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        if all(isinstance(x, str) for x in v):
            return "；".join(v)
        return "；".join(_val(x) for x in v)
    if isinstance(v, dict):
        return "，".join("%s=%s" % (k, _val(x)) for k, x in v.items())
    return json.dumps(v, ensure_ascii=False)


# Fields a case/reference/document keeps back until the agent asks for depth="full": they matter when reusing or
# verifying, not when deciding whether the entry answers the question.
DEFER = {"case": ("reuse", "setup", "findings"), "reference": ("data_values", "methods", "identifier"),
         "document": ("scope", "version_note")}


def read_text(rd, handles, depth="core"):
    out = []
    for it in rd["items"]:
        n = handles.get(it["entry_id"])
        head = "#%d %s %s" % (n, _t(it["type"]), it["title"])
        if it.get("status") and it["status"] != "active":
            head += "［%s］" % it["status"]
        lines = [head]
        held = []
        for k, v in it["fields"].items():
            if depth == "core" and k in DEFER.get(it["type"], ()):
                held.append(k)
                continue
            lines.append("%s: %s" % (k, _val(v)))
        if held:
            lines.append("（未展开：%s；需要时 read depth=full）" % "、".join(held))
        if it.get("conditions"):
            lines.append("适用条件: " + "；".join("%s %s %s%s" % (c["key"], c.get("op", "eq"), _val(c.get("value")),
                                                         c.get("unit") or "") for c in it["conditions"]))
        if it.get("sources"):
            srcs = [s if isinstance(s, str) else s.get("title", "") for s in it["sources"]]
            lines.append("来源: " + "；".join(s.split(" | ")[0] for s in srcs))
        if it.get("relations"):
            lines.append("关联: " + "；".join(it["relations"] if isinstance(it["relations"][0], str) else
                                             ["%s %s" % (r["label"], r["entry_id"]) for r in it["relations"]]))
        doc = it.get("document")
        if doc:
            files = doc["files"] if isinstance(doc["files"][0], str) else \
                ["%s [%s]" % (f["path"], f["role"]) for f in doc["files"]]
            lines.append("原文文件: " + "；".join(f.split(" ")[0] + (" 可复用" if "reusable" in f else "")
                                              for f in files) + "（read_original 读原文）")
        out.append("\n".join(lines))
    tail = ""
    if rd.get("next_cursor"):
        tail = "\n（还有未返回的条目）"
    return "\n\n".join(out) + tail
