#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""知识名片(LLM+wiki 模式): 记忆库 → 确定性骨架 + 可选 LLM 蒸馏正文。

数据源(全只读): memory/atoms.jsonl + scenarios.json + SOP.md + notes/ + LEDGER.jsonl。
产物: memory/knowledge_cards.jsonl(整体重建,派生件) + memory/knowledge_reviews.json(人工核状态,重建不丢)。

纪律:
- 骨架(事实/refs/统计)确定性生成,零网络;
- --llm 才调 Kimi 蒸馏正文,提示词钉死"只准复述给定事实,禁止引入新事实/数字",
  失败不阻断(卡片照出,正文留空,页面上事实区永远原文可见);
- reviewed 是人工动作(review 子命令),LLM 永远不能自己标已核。

用法:
  python3.11 evolution/knowledge_cards.py build [--llm]
  python3.11 evolution/knowledge_cards.py review <card_id> [--note 说明]
  python3.11 evolution/knowledge_cards.py list
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_MCP = os.path.join(os.path.dirname(HERE), "mcp")
if _MCP not in sys.path:
    sys.path.insert(0, _MCP)
import memory  # noqa: E402  (atoms/scenarios/LEDGER 常量与加载器)
import paths   # noqa: E402

CARDS_PATH_DEFAULT = os.path.join(memory.EXP_DIR, "knowledge_cards.jsonl")
REVIEWS_PATH_DEFAULT = os.path.join(memory.EXP_DIR, "knowledge_reviews.json")

KIMI_ENDPOINT = "https://api.kimi.com/coding/v1/messages"  # 与 mcp/model_research 同源
KIMI_MODEL = "k3"
BIGMODEL_ENDPOINT = "https://open.bigmodel.cn/api/anthropic/v1/messages"
BIGMODEL_MODEL = "glm-4.6"
MAX_FACTS = 8
MAX_REFS = 12
MAX_ATOM_IDS = 20

CATEGORY_RULES = [
    (("violation", "correction", "friction"), "摩擦与纠正"),
    (("lesson",), "教训"),
    (("verified",), "已验证结论"),
    (("wall", "audit", "bypass", "blindspot", "attack", "contract"), "安全"),
    (("outcome", "pipeline", "build", "engine", "forcefield", "protocol",
      "release", "handoff", "fix", "species", "validation"), "工程经验"),
]


def _category(kind):
    for keys, cat in CATEGORY_RULES:
        if any(k in kind for k in keys):
            return cat
    return "其他"


def _load_atoms(atoms_path):
    rows = []
    try:
        with open(atoms_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    a = json.loads(line)
                except Exception:
                    continue
                if isinstance(a, dict) and a.get("kind"):
                    rows.append(a)
    except OSError:
        pass
    return rows


def _dedup_keep_order(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _card_from_atoms(kind, atoms):
    facts = _dedup_keep_order([str(a.get("fact", ""))[:400] for a in atoms if a.get("fact")])
    refs = _dedup_keep_order([r for a in atoms for r in (a.get("refs") or [])])[:MAX_REFS]
    ts = [a.get("t") for a in atoms if isinstance(a.get("t"), (int, float))]
    return {
        "id": "kc-" + re.sub(r"[^A-Za-z0-9_-]", "-", kind)[:64],
        "source": "atoms",
        "kind": kind,
        "title": kind,
        "category": _category(kind),
        "count": len(atoms),
        "first_ts": min(ts) if ts else None,
        "last_ts": max(ts) if ts else None,
        "facts": facts[:MAX_FACTS],
        "refs": refs,
        "atom_ids": _dedup_keep_order([a.get("id") for a in atoms if a.get("id")])[:MAX_ATOM_IDS],
    }


def _sop_cards(sop_path):
    """SOP.md 按 '## ' 节拆卡(标题+正文摘要,确定性)。"""
    cards = []
    try:
        text = open(sop_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return cards
    sections = re.split(r"(?m)^##\s+", text)
    for sec in sections[1:]:
        lines = sec.splitlines()
        title = lines[0].strip() if lines else ""
        body = "\n".join(lines[1:]).strip()
        if not title or not body:
            continue
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", title)[:40]
        cards.append({
            "id": "kc-sop-" + (slug or "sec"),
            "source": "sop",
            "kind": "sop",
            "title": "SOP: " + title,
            "category": "SOP",
            "count": 1,
            "first_ts": None, "last_ts": None,
            "facts": [l.strip().lstrip("-* ").strip() for l in body.splitlines()
                      if l.strip() and not l.strip().startswith(("<!--", "#"))][:MAX_FACTS],
            "refs": ["SOP.md#" + title],
            "atom_ids": [],
        })
    return cards


def _note_cards(notes_dir):
    cards = []
    try:
        names = sorted(os.listdir(notes_dir))
    except OSError:
        return cards
    for name in names:
        if not name.endswith(".md"):
            continue
        try:
            text = open(os.path.join(notes_dir, name), encoding="utf-8",
                        errors="replace").read()
        except OSError:
            continue
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        body = ""
        for p in paras:
            if not p.startswith("<!--"):  # 跳过机读头
                body = p
                break
        body = re.sub(r"^#+\s*", "", body)[:400]
        if not body:
            continue
        title = re.sub(r"\.md$", "", name)
        cards.append({
            "id": "kc-note-" + re.sub(r"[^A-Za-z0-9_-]", "-", title)[:64],
            "source": "note",
            "kind": "note",
            "title": "笔记: " + title,
            "category": "研判笔记",
            "count": 1,
            "first_ts": None, "last_ts": None,
            "facts": [body],
            "refs": ["notes/" + name],
            "atom_ids": [],
        })
    return cards


def _cred_keys():
    """credentials.yaml 里全部 LLM key(env 优先同名覆盖)。"""
    keys = {}
    try:
        cred = os.path.join(paths.dsh_home(make=False), ".credentials.yaml")
        for line in open(cred, encoding="utf-8"):
            m = re.match(r'^(KIMI_API_KEY|BIGMODEL_ANTHROPIC_API_KEY):\s*"?([^"\n]+)"?\s*$',
                         line)
            if m:
                keys[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    for name in ("KIMI_API_KEY", "BIGMODEL_ANTHROPIC_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            keys[name] = v
    return keys


def _providers():
    """LLM 供应商链: Kimi 优先(同 model_research),失效自动落到 BigModel glm。"""
    keys = _cred_keys()
    chain = []
    if keys.get("KIMI_API_KEY"):
        chain.append(("kimi-k3", KIMI_ENDPOINT, KIMI_MODEL, keys["KIMI_API_KEY"]))
    if keys.get("BIGMODEL_ANTHROPIC_API_KEY"):
        chain.append(("glm-4.6", BIGMODEL_ENDPOINT, BIGMODEL_MODEL,
                      keys["BIGMODEL_ANTHROPIC_API_KEY"]))
    return chain


_DISTILL_SYSTEM = (
    "你是分子模拟经验库的 wiki 编辑,把原始观测事实蒸馏成一张知识名片。"
    "铁律:只准复述/归纳给定事实,禁止引入任何未在事实中出现的事实、数字或机制;"
    "事实不足时如实写'证据不足'。只输出严格 JSON,无其他文字:\n"
    '{"summary":"80字内结论","apply":["适用条件"],"dont_apply":["不适用情形"],'
    '"conditions":"体系/引擎/力场/规模边界"}'
)


def _extract_json(text):
    """剥代码围栏后解析首个完整 JSON 对象;截断/夹带文案返回 None。"""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def distill_card(card, timeout=120):
    """单卡 LLM 蒸馏,按供应商链逐个试;失败返回带 error 的 dict,不抛。"""
    chain = _providers()
    if not chain:
        return {"error": "no-api-key"}
    facts = "\n".join("- " + f for f in card["facts"])
    last_err = "no-provider"
    for pname, endpoint, model, key in chain:
        body = {
            # glm 思考链消耗输出预算,900 会截断 JSON → 2000
            "model": model, "max_tokens": 2000,
            "system": _DISTILL_SYSTEM,
            "messages": [{"role": "user", "content":
                          "经验类别: %s\n事实(%d 条):\n%s" % (card["kind"], len(card["facts"]), facts)}],
        }
        try:
            req = urllib.request.Request(
                endpoint, data=json.dumps(body).encode(),
                headers={"x-api-key": key, "authorization": "Bearer " + key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            text = "".join(b.get("text", "") for b in payload.get("content", [])
                           if b.get("type") == "text").strip()
            data = _extract_json(text)
            if not data or not data.get("summary"):
                last_err = "bad-llm-json(%s)" % pname
                continue
            return {
                "summary": str(data.get("summary", ""))[:200],
                "apply": [str(x)[:120] for x in (data.get("apply") or [])][:4],
                "dont_apply": [str(x)[:120] for x in (data.get("dont_apply") or [])][:4],
                "conditions": str(data.get("conditions", ""))[:200],
                "model": pname, "ts": time.time(),
            }
        except urllib.error.HTTPError as e:
            last_err = "http%s@%s" % (e.code, pname)
            if e.code in (401, 403):
                continue  # 凭证失效 → 换下一家
        except Exception as e:
            last_err = "%s@%s" % (str(e)[:80], pname)
    return {"error": last_err[:160]}


def load_reviews(reviews_path=REVIEWS_PATH_DEFAULT):
    try:
        return json.load(open(reviews_path, encoding="utf-8"))
    except Exception:
        return {}


def build(cards_path=CARDS_PATH_DEFAULT, atoms_path=None, sop_path=None,
          notes_dir=None, scenarios_path=None, with_llm=False):
    """全量重建知识名片。路径参数缺省用 memory 模块常量(测试可注入沙盒)。"""
    atoms_path = atoms_path or memory.ATOMS
    sop_path = sop_path or os.path.join(memory.EXP_DIR, "SOP.md")
    notes_dir = notes_dir or os.path.join(memory.EXP_DIR, "notes")
    scenarios_path = scenarios_path or memory.SCENARIOS

    atoms = _load_atoms(atoms_path)
    by_kind = {}
    for a in atoms:
        by_kind.setdefault(a["kind"], []).append(a)

    cards = [_card_from_atoms(k, v) for k, v in by_kind.items()]
    cards.extend(_sop_cards(sop_path))
    cards.extend(_note_cards(notes_dir))

    # 场景聚合计数并进同名 atom 卡(scenarios 由 rebuild() 维护,只是聚合视图)
    try:
        scenarios = json.load(open(scenarios_path, encoding="utf-8"))
        for s in scenarios if isinstance(scenarios, list) else []:
            sig = s.get("signature")
            for c in cards:
                if c["source"] == "atoms" and c["kind"] == sig:
                    c["scenario_count"] = s.get("count", c["count"])
    except Exception:
        pass

    if with_llm:
        # 增量缓存:同 id 且 facts 未变的旧卡复用蒸馏结果,重跑只补缺(省费用)
        prev = {}
        try:
            with open(cards_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            pc = json.loads(line)
                            if isinstance(pc, dict) and pc.get("distill"):
                                prev[pc["id"]] = (pc["facts"], pc["distill"])
                        except Exception:
                            pass
        except OSError:
            pass
        for c in cards:
            if not c["facts"]:
                continue
            old = prev.get(c["id"])
            if old and old[0] == c["facts"]:
                c["distill"] = old[1]
                continue
            d = distill_card(c)
            if "summary" in d:
                c["distill"] = d
            else:
                c["distill_error"] = d.get("error", "unknown")
    for c in cards:
        c["generated_ts"] = time.time()

    reviews = load_reviews(os.path.join(os.path.dirname(cards_path),
                                        "knowledge_reviews.json"))
    for c in cards:
        r = reviews.get(c["id"])
        if r:
            c["reviewed_by"] = r.get("by")
            c["reviewed_ts"] = r.get("ts")

    order = {"已验证结论": 0, "教训": 1, "SOP": 2, "工程经验": 3,
             "研判笔记": 4, "摩擦与纠正": 5, "安全": 6, "其他": 7}
    cards.sort(key=lambda c: (order.get(c["category"], 9), -c["count"], c["id"]))

    tmp = cards_path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        for c in cards:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, cards_path)
    return cards


def review(card_id, by=None, note="", cards_path=CARDS_PATH_DEFAULT,
           reviews_path=REVIEWS_PATH_DEFAULT):
    """人工核卡:记录 who/ts/note。LLM 无权调用此函数以外的方式改 reviews。"""
    ids = set()
    try:
        with open(cards_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        ids.add(json.loads(line).get("id"))
                    except Exception:
                        pass
    except OSError:
        pass
    if card_id not in ids:
        raise SystemExit("卡片不存在: %s(先跑 build;list 看全部 id)" % card_id)
    reviews = load_reviews(reviews_path)
    reviews[card_id] = {"by": by or os.environ.get("USER", "human"),
                        "ts": time.time(), "note": note}
    tmp = reviews_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=1)
    os.replace(tmp, reviews_path)
    return reviews[card_id]


def main():
    ap = argparse.ArgumentParser(description="知识名片: 记忆库 → wiki 卡片")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="重建卡片(--llm 附带 Kimi 蒸馏正文)")
    b.add_argument("--llm", action="store_true")
    r = sub.add_parser("review", help="人工核卡(标记已核)")
    r.add_argument("card_id")
    r.add_argument("--note", default="")
    r.add_argument("--by", default=None)
    sub.add_parser("list", help="列出卡片")
    args = ap.parse_args()

    if args.cmd == "build":
        cards = build(with_llm=args.llm)
        n_llm = sum(1 for c in cards if c.get("distill"))
        print("知识名片 %d 张(蒸馏 %d 张)→ %s" % (len(cards), n_llm, CARDS_PATH_DEFAULT))
    elif args.cmd == "review":
        rec = review(args.card_id, by=args.by, note=args.note)
        print("已核: %s by %s %s" % (args.card_id, rec["by"],
                                     datetime.datetime.fromtimestamp(rec["ts"]).strftime("%F %T")))
    else:
        try:
            lines = open(CARDS_PATH_DEFAULT, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            lines = []
        reviews = load_reviews()
        for line in lines:
            if not line.strip():
                continue
            try:
                c = json.loads(line)
            except Exception:
                continue
            mark = "✓已核" if c["id"] in reviews else ("LLM" if c.get("distill") else "事实")
            print("%-4s %-12s %3d次  %s" % (mark, c["category"], c["count"], c["id"]))


if __name__ == "__main__":
    main()
