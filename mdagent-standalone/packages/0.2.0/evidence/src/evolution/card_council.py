#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""经验研讨团(分布式讨论智能体): 对知识名片做 五角色发言 + 主席裁决 的双轴归类。

轴 1 按智能体体系(五层): 对话层 / 编排层 / 监控层 / 恢复层 / 演化治理 + 跨层通用。
轴 2 按物理过程: 热与能量 / 物质守恒 / 电荷平衡与收敛 / 键合与拓扑 / 数值稳定 /
建模与前处理 / 流程治理(非物理) / 其他。

纪律(与 knowledge_cards 同源):
- 输入只读 knowledge_cards.jsonl;提示词钉死"只准复述给定事实,禁止引入新事实";
- LLM 失败/未开/--no-llm 一律降级确定性关键词规则基线,全覆盖恰好一次;
- 产物 memory/knowledge_taxonomy.json 是派生视图(原子写),绝不改卡片本体;
- 主席输出经覆盖率校验:缺失/越界的卡 id 一律回填基线裁决,LLM 不能丢卡。

用法:
  python3.11 evolution/card_council.py run [--llm]
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_MCP = os.path.join(os.path.dirname(HERE), "mcp")
if _MCP not in sys.path:
    sys.path.insert(0, _MCP)
import memory  # noqa: E402

sys.path.insert(0, HERE)
from knowledge_cards import _extract_json, _providers  # noqa: E402

CARDS_PATH_DEFAULT = os.path.join(memory.EXP_DIR, "knowledge_cards.jsonl")
TAXONOMY_PATH_DEFAULT = os.path.join(memory.EXP_DIR, "knowledge_taxonomy.json")
COUNCIL_CACHE_DEFAULT = os.path.join(memory.EXP_DIR, "knowledge_council_cache.json")
MAX_FACTS_PER_CARD = 3
MAX_CARDS = 200

# 五角色 = 用户的智能体体系五层;brief 同时是确定性规则的 keyword 源
ROLES = [
    {"key": "dialog", "name": "对话层(dsh 主对话)",
     "brief": "persona/flow-gate 闸、用户纠正、轮级摩擦、问答质量"},
    {"key": "pipeline", "name": "编排层(MD Exp 管线)",
     "brief": "五段工作流:建模/引擎/力场/协议/交付,SCNet 提交,环境与构建"},
    {"key": "monitor", "name": "监控层(web_monitor)",
     "brief": "巡检事件流、异常检测、管线状态、指标与看板"},
    {"key": "recovery", "name": "恢复层(recover_agent)",
     "brief": "恢复策略库、金丝雀专家、根因、熔断与退避"},
    {"key": "evolution", "name": "演化治理(observe/门禁)",
     "brief": "经验沉淀、提案/门禁/回滚、安全红线、蜂群研究"},
]

LAYER_RULES = [
    ("dialog", ("flow-gate", "user-correction", "repeat-question", "friction",
                "persona", "violation", "correction", "intent")),
    ("recovery", ("recovery", "strategy", "canary", "swarm", "repair")),
    ("monitor", ("monitor", "pipeline", "outcome")),
    ("evolution", ("evolution", "proposal", "attack", "bypass", "blindspot",
                   "audit", "wall", "kb", "ledger", "gate")),
    ("pipeline", ("build", "engine", "forcefield", "protocol", "release",
                  "species", "validation", "handoff", "scnet", "hpc", "env-",
                  "gromacs", "packmol", "model")),
]
CROSS_LAYER = {"key": "cross", "name": "跨层通用", "summary": "SOP/笔记等横切经验。"}

PHYSICS = [
    {"key": "thermo_energy", "name": "热与能量"},
    {"key": "mass_conservation", "name": "物质守恒"},
    {"key": "charge_convergence", "name": "电荷平衡与收敛"},
    {"key": "bond_topology", "name": "键合与拓扑"},
    {"key": "numerical_stability", "name": "数值稳定"},
    {"key": "modeling_setup", "name": "建模与前处理"},
    {"key": "governance", "name": "流程治理(非物理)"},
    {"key": "other", "name": "其他"},
]
PHYSICS_KEYS = [p["key"] for p in PHYSICS]

PHYSICS_RULES = [
    ("thermo_energy", ("thermal", "温度", "过热", "热失控", "T_SPIKE", "temp",
                       "加热", "heating", "thermo")),
    ("mass_conservation", ("lost atoms", "丢原子", "原子丢失", "LOST_ATOMS",
                           "原子守恒", "原子数", "lost-atoms")),
    ("charge_convergence", ("qeq", "电荷", "收敛", "convergence", "tolerance",
                            "eqeq")),
    ("bond_topology", ("断链", "bond", "overlap", "重叠", "拓扑", "键")),
    ("numerical_stability", ("nan", "发散", "divergence", "数值爆", "inf",
                             "不稳定")),
    ("modeling_setup", ("packmol", "建模", "盒子", "min ", "npt", "力场",
                        "forcefield", "结构", "弛豫", "relax")),
    ("governance", ("审批", "闸", "流程", "工具错误", "复发", "中断", "提案",
                    "策略", "权限", "回滚", "飞书")),
]


def load_cards(cards_path=None):
    cards = []
    path = cards_path or CARDS_PATH_DEFAULT
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    c = json.loads(line)
                except Exception:
                    continue
                if isinstance(c, dict) and c.get("id"):
                    cards.append(c)
    except OSError:
        pass
    return cards[:MAX_CARDS]


def _match(rules, text):
    low = text.lower()
    for key, words in rules:
        if any(w.lower() in low for w in words):
            return key
    return None


def baseline_taxonomy(cards):
    """确定性基线:keyword 规则双轴归类,全覆盖恰好一次。"""
    layers = {r["key"]: [] for r in ROLES}
    layers["cross"] = []
    physics = {p["key"]: [] for p in PHYSICS}
    for c in cards:
        text = " ".join([str(c.get("kind", "")), str(c.get("title", ""))]
                        + [str(f) for f in (c.get("facts") or [])[:MAX_FACTS_PER_CARD]])
        layers[_match(LAYER_RULES, text) or "cross"].append(c["id"])
        physics[_match(PHYSICS_RULES, text) or "other"].append(c["id"])
    return {
        "layers": [{"key": k, "name": (next(r["name"] for r in ROLES
                                            if r["key"] == k)
                                       if k != "cross" else CROSS_LAYER["name"]),
                    "summary": "", "card_ids": v}
                   for k, v in layers.items() if v],
        "physics": [{"key": p["key"], "name": p["name"], "summary": "",
                     "card_ids": physics[p["key"]]}
                    for p in PHYSICS if physics[p["key"]]],
    }


_ROLE_SYSTEM = (
    "你是分子模拟多智能体体系里的「%s」智能体,职责:%s。"
    "研讨纪律:只准复述/归纳下面给定的卡片事实,禁止引入未出现的事实、数字或机制。"
    "从你这一层的视角审视全部知识名片,只挑与你层相关的卡,不许多、不要 tags。"
    "只输出严格 JSON,无其他文字:\n"
    '{"selected":["相关卡片id"],"summary":"60字内本层经验总结"}'
)


def _chat(system, user, timeout=180, max_tokens=6000):
    """单轮 LLM 调用,供应商链逐个试;失败返回 None。"""
    chain = _providers()
    if not chain:
        return None
    last_err = "no-provider"
    for pname, endpoint, model, key in chain:
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        try:
            import urllib.error
            import urllib.request
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
            if data is not None:
                data["_provider"] = pname
                return data
            last_err = "bad-json@%s" % pname
        except urllib.error.HTTPError as e:
            last_err = "http%s@%s" % (e.code, pname)
            if e.code in (401, 403):
                continue
        except Exception as e:
            last_err = "%s@%s" % (str(e)[:80], pname)
    sys.stderr.write("council chat failed: %s\n" % last_err)
    return None


def _cards_digest(cards):
    """带 1 起序号的紧凑清单:主席按序号归组,输出体积比长 id 小一个量级。"""
    rows = [{"i": n, "kind": c.get("kind", ""), "title": c.get("title", ""),
             "facts": [str(f)[:160] for f in (c.get("facts") or
                                              [])[:MAX_FACTS_PER_CARD]]}
            for n, c in enumerate(cards, 1)]
    return json.dumps(rows, ensure_ascii=False)


def _idx_map(cards):
    return {n: c["id"] for n, c in enumerate(cards, 1)}


def _role_call_with(role, cards, out):
    """归一化角色发言:序号→id、越界剔除(主席与基线兜底覆盖)。"""
    idx = _idx_map(cards)
    selected = [idx[i] for i in (out.get("selected") or [])
                if isinstance(i, int) and i in idx][:MAX_CARDS]
    return {"role": role["key"], "selected": selected,
            "summary": str(out.get("summary", ""))[:200],
            "provider": out.get("_provider", "")}


def _role_call(role, cards):
    out = _chat(_ROLE_SYSTEM % (role["name"], role["brief"]),
                "卡片事实清单(%d 张,selected 填卡序号 i):\n%s"
                % (len(cards), _cards_digest(cards)))
    if not out:
        return {"role": role["key"], "error": "llm-failed"}
    return _role_call_with(role, cards, out)


_CHAIR_SYSTEM = (
    "你是经验研讨团主席。五个角色的层视角发言与全量卡片事实如下。"
    "铁律:只准复述给定事实;每张卡在两个轴上各恰好归入一组;分组一律填卡序号"
    "(i),不填长 id;物理 key 只准从词表 %s 选;层 key 只准从 %s 选"
    "(横切经验归 cross)。分歧按事实裁决。"
    "输出必须用最紧凑 JSON(不要数组套对象,不要解释):\n"
    '{"l":{"层key":[1,5]},"p":{"物理key":[2]},"s":{"层key":"30字内层总结"},'
    '"note":"40字内总裁决说明"}'
)


def _chair_call(roles_out, cards):
    role_keys = ",".join(r["key"] for r in ROLES) + ",cross"
    prompt = ("角色发言:\n%s\n\n卡片事实清单:\n%s"
              % (json.dumps(roles_out, ensure_ascii=False)[:20000],
                 _cards_digest(cards)))
    # 主席要吐全部卡的双轴归组,glm 思考链重:败一次再加档重试
    out = _chat(_CHAIR_SYSTEM % (",".join(PHYSICS_KEYS), role_keys),
                prompt, max_tokens=8000)
    if out is None:
        out = _chat(_CHAIR_SYSTEM % (",".join(PHYSICS_KEYS), role_keys),
                    prompt, max_tokens=16000)
    return out


def _norm_chair(chair, cards):
    """紧凑模式 {"l":{key:[序号]}} → 标准分组列表(序号映射回 id,越界丢弃)。"""
    idx = _idx_map(cards)

    def groups(axis):
        out = []
        src = chair.get(axis)
        if not isinstance(src, dict):
            return out
        for k, v in src.items():
            if isinstance(v, list):
                out.append({"key": str(k), "summary": "",
                            "card_ids": [idx[i] for i in v
                                         if isinstance(i, int) and i in idx]})
        return out

    layers, physics = groups("l"), groups("p")
    sums = {k: str(v)[:200] for k, v in (chair.get("s") or {}).items()
            if isinstance(v, str)} if isinstance(chair.get("s"), dict) else {}
    # 兼容宽松形式(长 id 数组套对象)
    if not layers and isinstance(chair.get("layers"), list):
        known = {c["id"] for c in cards}
        for g in chair["layers"]:
            if isinstance(g, dict):
                g["card_ids"] = [i for i in (g.get("card_ids") or [])
                                 if isinstance(i, str) and i in known]
                layers.append(g)
    if not physics and isinstance(chair.get("physics"), list):
        known = {c["id"] for c in cards}
        for g in chair["physics"]:
            if isinstance(g, dict):
                g["card_ids"] = [i for i in (g.get("card_ids") or [])
                                 if isinstance(i, str) and i in known]
                physics.append(g)
    for g in layers + physics:
        if isinstance(g, dict):
            g["summary"] = sums.get(g.get("key"), g.get("summary", "")) or ""
    return {"layers": layers, "physics": physics, "note": chair.get("note", "")}


def _merge_chair(chair_raw, base, cards):
    chair = _norm_chair(chair_raw, cards)
    """主席输出过覆盖率校验:未知 id/重复归组剔除,缺口回填基线。"""
    known = {c["id"] for c in cards}
    layer_names = {r["key"]: r["name"] for r in ROLES}
    layer_names["cross"] = CROSS_LAYER["name"]

    def norm(axis, keys_ok, names):
        seen = {}
        for g in (chair.get(axis) or []):
            if not isinstance(g, dict) or g.get("key") not in keys_ok:
                continue
            k = g["key"]
            if k not in seen:
                seen[k] = {"key": k, "name": names[k],
                           "summary": str(g.get("summary", ""))[:200],
                           "card_ids": []}
            for i in (g.get("card_ids") or []):
                if isinstance(i, str) and i in known and \
                        i not in {x for ids in seen.values()
                                  for x in ids["card_ids"]}:
                    seen[k]["card_ids"].append(i)
        assigned = {i for g in seen.values() for i in g["card_ids"]}
        for g in base[axis]:
            if g["key"] not in seen:
                seen[g["key"]] = {"key": g["key"], "name": g["name"],
                                  "summary": "", "card_ids": []}
            for i in g["card_ids"]:
                if i not in assigned:
                    seen[g["key"]]["card_ids"].append(i)
                    assigned.add(i)
        # 顺序按基线,但基线空组的键(主席可能用到)不得把整组丢掉
        order = [x["key"] for x in base[axis]]
        extras = [k for k in seen if k not in order]
        return [seen[k] for k in (order + extras)
                if k in seen and seen[k]["card_ids"]]

    taxonomy = {"layers": norm("layers", set(layer_names), layer_names),
                "physics": norm("physics", set(PHYSICS_KEYS),
                                {p["key"]: p["name"] for p in PHYSICS}),
                "note": str(chair.get("note", ""))[:120]}
    return taxonomy


def _taxonomy_from_roles(roles_out, base, cards):
    """主席缺席的降级聚合:层轴按五角色发言(先到先得),物理轴走规则基线。"""
    by_id = {c["id"] for c in cards}
    assign = {}
    for r in roles_out:
        for cid in (r.get("selected") or []):
            if cid in by_id and cid not in assign:
                assign[cid] = r["role"]
    summaries = {r["role"]: r.get("summary", "")
                 for r in roles_out if not r.get("error")}
    layers = []
    for role in ROLES:
        ids = [i for i, k in assign.items() if k == role["key"]]
        if ids:
            layers.append({"key": role["key"], "name": role["name"],
                           "summary": summaries.get(role["key"], ""),
                           "card_ids": ids})
    cross = [c["id"] for c in cards if c["id"] not in assign]
    if cross:
        layers.append({"key": "cross", "name": CROSS_LAYER["name"],
                       "summary": "", "card_ids": cross})
    return {"layers": layers, "physics": base["physics"],
            "note": "主席 LLM 未出,按五角色发言聚合(先到先得)"}


def _fingerprint(cards):
    import hashlib
    return hashlib.sha1(_cards_digest(cards).encode("utf-8")).hexdigest()[:16]


def _load_cache(path):
    try:
        v = json.load(open(path, encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _save_cache(cache, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, path)


def council(cards_path=None, taxonomy_path=None, with_llm=False, timeout=120,
            cache_path=None):
    cards = load_cards(cards_path)
    taxonomy = baseline_taxonomy(cards)
    result = {"generated_ts": time.time(), "llm": False, "mode": "baseline",
              "roles": [], "cards_total": len(cards)}
    if with_llm and cards:
        cache_path = cache_path or COUNCIL_CACHE_DEFAULT
        fp = _fingerprint(cards)
        cache = _load_cache(cache_path)
        dirty = False
        # 角色发言缓存:成功的按指纹复用,重跑只补失败的(LLM 抖动不清零成果)
        roles_out = []
        for r in ROLES:
            key = "role:%s:%s" % (r["key"], fp)
            cached = cache.get(key)
            if isinstance(cached, dict) and not cached.get("error"):
                roles_out.append(cached)
                continue
            out = _role_call(r, cards)
            if out.get("error"):  # 单次失败重试一次(截断/超时常见)
                out = _role_call(r, cards)
            cache[key] = out
            dirty = True
            roles_out.append(out)
        result["roles"] = roles_out
        chair = None
        ckey = "chair:%s" % fp
        cached_chair = cache.get(ckey)
        if isinstance(cached_chair, dict):
            chair = cached_chair
        else:
            chair = _chair_call(roles_out, cards)
            if isinstance(chair, dict):
                cache[ckey] = chair
                dirty = True
        if dirty:
            _save_cache(cache, cache_path)
        if chair:
            taxonomy = _merge_chair(chair, taxonomy, cards)
            result["llm"] = True
            result["mode"] = "roles+chair"
        elif any(not r.get("error") for r in roles_out):
            taxonomy = _taxonomy_from_roles(roles_out, taxonomy, cards)
            result["llm"] = True
            result["mode"] = "roles-only"
    result.update(taxonomy)

    path = taxonomy_path or TAXONOMY_PATH_DEFAULT
    tmp = "%s.tmp.%d" % (path, os.getpid())
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return result


def main():
    ap = argparse.ArgumentParser(description="经验研讨团: 知识名片双轴归类")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="跑一轮研讨(--llm 才真调 LLM)")
    r.add_argument("--llm", action="store_true")
    args = ap.parse_args()
    t = council(with_llm=args.llm)
    mode = {"roles+chair": "五角色+主席 LLM 研讨",
            "roles-only": "五角色 LLM 研讨(主席缺席,按角色发言聚合)",
            "baseline": "确定性规则基线(未跑 LLM)"}[t.get("mode", "baseline")]
    print("研讨产物 → %s [%s] 卡片 %d 张" % (TAXONOMY_PATH_DEFAULT, mode,
                                             t["cards_total"]))
    for axis in ("layers", "physics"):
        for g in t[axis]:
            print("  %-14s %-22s %3d 张  %s"
                  % (axis, g["name"], len(g["card_ids"]),
                     (g.get("summary") or "")[:60]))
    if t.get("note"):
        print("主席裁决: %s" % t["note"])


if __name__ == "__main__":
    main()
