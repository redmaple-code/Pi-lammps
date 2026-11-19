#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演化训练记忆(借鉴 TencentDB-Agent-Memory 的分层记忆模式,stdlib)
================================================================
L0 原始层:既有的 _flowgate.jsonl / LEDGER.jsonl(本模块只读,不写)。
L1 原子层:experience/atoms.jsonl —— 一条=一个失效/修复事实,带 refs 指回 L0。
L2 场景层:experience/scenarios.json —— 同签名原子聚成复发场景,挂历史修复结果。
L3 规则层:experience/SOP.md —— 从"修复已生效且未回滚"的场景蒸馏出的规则,
         人可读、白盒;每行标注 atom/ledger id,可逐层 drill-down 回原始证据。

纪律:压缩必须可溯源(TencentDB 原则:下层存证据,上层存结构);
SOP.md 每次全量重建(确定性),绝不增量涂抹导致来源断链。
"""
import json
import os
import sys
import time
import uuid

try:
    import fcntl
except ImportError:  # 非 POSIX:退化为无锁(部署目标为 Linux,仅保留可移植性)
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mcp"))
import paths

# 数据根外置(2026-08-29):常量名保留为测试猴子补丁入口
EXP_DIR = paths.memory_dir()
ATOMS = os.path.join(EXP_DIR, "atoms.jsonl")
SCENARIOS = os.path.join(EXP_DIR, "scenarios.json")
SOP = os.path.join(EXP_DIR, "SOP.md")
LEDGER = os.path.join(EXP_DIR, "LEDGER.jsonl")

# ledger 的提案 class → 失效签名(原子 kind)映射:两命名空间不同,缺了它
# SOP 永远"尚无生效修复"(2026-08-29 对抗审计发现断链)
CLASS_TO_SIGNATURES = {
    "flow_gate_intent_terms": ["flow-gate-violation"],
    "persona_wording": ["repeat-question", "user-correction"],
    "pipeline_steps": ["pipeline_error"],
    "monitor_logic": ["monitor_critical"],
}


def _atomic_write(path, text):
    """tmp(pid+uuid)+os.replace,与 P3 管线快照同口径(八轮对抗:rebuild 半写文件)"""
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    _group_writable(path)


def _group_writable(path):
    """接缝3配套(2026-09-15): 整写后新文件回落调用方 umask(644), mdorch 侧
    (编排器自动写记忆)会再次失去写权——统一抬回 664。目录带 setgid,
    新文件组天然是 mdorch, 664 即双主可写。失败静默(独占场景无影响)。"""
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass


def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)  # 合法 JSON 但非 dict(如 123)同样击穿 .get,统一过滤(四轮对抗)
    return rows


def load_atoms():
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_atoms()
    # E4(2026-09-14): 已撤回原子(retracted)对全部查询不可见——包括 rebuild/名片/
    # 去重;本体保留在文件中(审计不删除)
    # 接缝修复八(2026-09-15): superseded 原子(被蒸馏结论取代)同样不可见——
    # 蒸馏后场景计数净减少, 不再新旧两套并存虚高复发数
    return [r for r in _read_jsonl(ATOMS) if not r.get("retracted") and not r.get("superseded_by")]


def retract_atom(atom_id, reason=""):
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.retract_atom(atom_id, reason)
    """E4(2026-09-14): 标记原子为已撤回(不删除, 后续查询经 load_atoms 过滤)。
    文件锁内整写; 撤回后重建 L2/L3 使 scenarios/SOP 立即跟上。"""
    return _flag_atom(atom_id, {"retracted": True,
                                "retract_reason": str(reason)[:200],
                                "retracted_at": time.time()})


def supersede_atom(old_id, new_id, reason=""):
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.supersede_atom(old_id, new_id, reason)
    """接缝修复八(2026-09-15): 标记旧原子被新原子取代(不删除, 查询过滤)。
    与 retract 的区别: retract=事实错了; supersede=事实没错但已被更高层结论吸收。"""
    return _flag_atom(old_id, {"superseded_by": str(new_id),
                               "supersede_reason": str(reason)[:200],
                               "superseded_at": time.time()})


def _flag_atom(atom_id, fields):
    """retract/supersede 共用: 文件锁内给指定原子打标记并整写, 然后重建 L2/L3。"""
    if fcntl is None:
        raise RuntimeError("_flag_atom 需要 POSIX fcntl(当前平台不可用)")
    with open(ATOMS + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        atoms = _read_jsonl(ATOMS)
        hit = False
        for a in atoms:
            if a.get("id") == atom_id:
                a.update(fields)
                hit = True
                break
        if not hit:
            return False
        tmp = "%s.tmp.%d" % (ATOMS, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            for a in atoms:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
        os.replace(tmp, ATOMS)
        _group_writable(ATOMS)
    rebuild()  # 标记立即传导到场景/SOP 视图
    return True


def current_value(kind):
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.current_value(kind)
    """接缝修复八: 返回某 kind 的最新有效原子(未撤回/未取代的最新一条), 无则 None。"""
    matching = [a for a in load_atoms() if a.get("kind") == kind]
    if not matching:
        return None
    return max(matching, key=lambda x: x.get("t", 0))


def find_scenario_semantic(query, top_k=5):
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.find_scenario_semantic(query, top_k)
    """接缝修复九(2026-09-15): 语义搜索——按内容关键词重叠匹配, 不只 kind 精确匹配。
    纯关键词重叠(零依赖), 按 (重叠度, 新鲜度) 排序返回 top_k 条原子。"""
    keywords = set(str(query).lower().split())
    scored = []
    for a in load_atoms():
        fact = str(a.get("fact", "")).lower()
        k = str(a.get("kind", "")).lower()
        overlap = sum(1 for kw in keywords if kw in fact or kw in k)
        if overlap > 0:
            scored.append((overlap, a))
    scored.sort(key=lambda x: (-x[0], -x[1].get("t", 0)))
    return [a for _, a in scored[:top_k]]


DECAY_HALF_LIFE_DAYS = 30  # 接缝修复十: 时效半衰期


def atom_weight(atom):
    """接缝修复十(2026-09-15): 原子的时效权重(越久越轻, 半衰期 30 天)。"""
    age_days = (time.time() - atom.get("t", 0)) / 86400
    return 0.5 ** (age_days / DECAY_HALF_LIFE_DAYS)


def find_scenario_weighted(signature):
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.find_scenario_weighted(signature)
    """接缝修复十: 带时效权重的场景查找——同签名原子按权重排序, 最新的权重最高。"""
    matching = [a for a in load_atoms() if a.get("kind") == signature]
    if not matching:
        return None
    weighted = [(atom_weight(a), a) for a in matching]
    weighted.sort(key=lambda x: -x[0])
    best = weighted[0]
    return {"signature": signature, "count": len(matching),
            "best": best[1], "weight": best[0],
            "atom_ids": [a.get("id") for _, a in weighted[:5]]}


def record_atom(kind, fact, refs=None):
    """追加一条 L1 原子记忆。kind 是失效签名(如 repeat-question / flow-gate-violation)。
    幂等:同一 L0 事件(由 refs 里的来源行标识)重复观测不重复入账;
    id 保证全局唯一(毫秒撞号时自增避让)。跨进程去重由文件锁保护(五轮对抗)。
    接缝修复十一(2026-09-15)写入验证, 验证不过返回 None(调用方须容忍):
      ① fact<10 字符——太短不是有效知识;
      ② 近 20 条内同 kind 且 fact 前 50 字符相同——近似重复, 返回已有原子;
      ③ friction 工具错误/打断的纯调用日志(含 turn+session 元数据)——噪音不是知识。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.record_atom(kind, fact, refs)
    os.makedirs(EXP_DIR, exist_ok=True)
    fact = str(fact)  # 观测源字段可能是 int 等非字符串(三轮对抗),切片前强转
    if len(fact) < 10:
        return None
    if len(fact) > 2000:
        fact = fact[:2000]
    if kind in ("friction-tool-error", "friction-interrupt"):
        if "turn" in fact and "session" in fact:
            return None
    if isinstance(refs, str):
        refs = [refs]  # 红队μ:str 型 refs 直接 list() 会逐字符拆分,静默丢溯源
    refs = [str(r) for r in list(refs or [])[:8]]
    if fcntl is None:
        return _record_atom_locked(kind, fact, refs)
    with open(ATOMS + ".lock", "a") as lock:
        # check-then-act 竞态:并发 observe 实例下同事件曾重复入账、复发计数虚高
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _record_atom_locked(kind, fact, refs)


def _record_atom_locked(kind, fact, refs):
    existing = load_atoms()
    for a in existing:
        if a.get("kind") == kind and a.get("fact") == fact[:400] and a.get("refs") == refs:
            return a
    # 修复十一②: 近似去重——近 20 条同 kind 且 fact 前 50 字符相同, 返回已有原子
    # (crash 类 fact 内嵌 step/temp, 不同事件前 50 字符即分叉, 不会误并复发计数)
    for a in existing[-20:]:
        if a.get("kind") == kind and str(a.get("fact", ""))[:50] == fact[:50]:
            return a
    taken = {a.get("id") for a in existing}
    ms = int(time.time() * 1000)
    while "a%d" % ms in taken:
        ms += 1
    # 并发撞号:毫秒+快照避让在同毫秒多写下仍会撞(实测 30 线程仅 21 唯一),
    # 加 uuid 短缀保证全局唯一(四轮对抗)
    atom = {"id": "a%d_%s" % (ms, uuid.uuid4().hex[:6]), "t": time.time(),
            "kind": kind, "fact": fact[:400], "refs": refs}
    # v1.6.0 三轴打标(课题/方法/运行):确定性规则零成本,打标器故障不阻断入账
    try:
        import tagging
        atom["axes"] = tagging.tag_atom(kind, atom["fact"], refs)
    except Exception:
        pass
    with open(ATOMS, "a", encoding="utf-8") as f:
        f.write(json.dumps(atom, ensure_ascii=False) + "\n")
    _group_writable(ATOMS)  # 新建兜底: 首次 append 建文件是调用方 umask
    return atom


def rebuild():
    """L1→L2 聚类 + L2→L3 蒸馏,全量确定性重建。返回场景列表。"""
    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.rebuild()
    atoms = load_atoms()
    ledger = _read_jsonl(LEDGER)
    # LEDGER 里每个 class 的修复结果(AUTO_APPLIED/APPLIED/ROLLED_BACK…)
    fixes_by_class = {}
    for row in ledger:
        meta = row.get("meta")
        cls = row.get("class") or (meta.get("class") if isinstance(meta, dict) else None)
        if cls:
            fixes_by_class.setdefault(cls, []).append(
                {"id": row.get("id"), "result": row.get("result"),
                 "ts": row.get("ts") or row.get("t")})

    scenarios = {}
    for a in atoms:
        sig = a.get("kind")
        if not sig or not a.get("id"):
            continue  # schema 坏行(缺 kind/id)跳过,不得拖垮重建(二轮对抗)
        sc = scenarios.setdefault(sig, {"signature": sig, "count": 0,
                                        "atom_ids": [], "examples": []})
        sc["count"] += 1
        sc["atom_ids"].append(str(a["id"]))  # 非 str id 强转:下游 join 不再 TypeError(五轮对抗)
        if len(sc["examples"]) < 3:
            sc["examples"].append(str(a.get("fact", ""))[:120])

    os.makedirs(EXP_DIR, exist_ok=True)
    _atomic_write(SCENARIOS, json.dumps(list(scenarios.values()),
                                        ensure_ascii=False, indent=2))

    lines = ["# 演化训练记忆 · SOP(自动生成,勿手改;证据链见 experience/atoms.jsonl 与 LEDGER.jsonl)",
             ""]
    for sig, sc in sorted(scenarios.items(), key=lambda kv: -kv[1]["count"]):
        fixes = [x for cls, xs in fixes_by_class.items()
                 if sig in CLASS_TO_SIGNATURES.get(cls, [cls]) for x in xs]
        # 2026-09-15: LEDGER 手工结账条目可能无 id(只有 slug)——回退 slug,不得炸 rebuild
        _xid = lambda x: str(x.get("id") or x.get("slug") or "?")  # noqa: E731
        applied = [x for x in fixes if x.get("result") in ("AUTO_APPLIED", "APPLIED")]
        rolled = {_xid(x) for x in fixes if x.get("result") == "ROLLED_BACK"}
        effective = [x for x in applied if _xid(x) not in rolled]
        status = "已生效修复 %s" % ",".join(_xid(x) for x in effective) if effective else "尚无生效修复"
        lines.append("## %s(复发 %d 次)" % (sig, sc["count"]))
        lines.append("- 典型: %s" % " / ".join(sc["examples"]))
        lines.append("- 修复: %s" % status)
        lines.append("- 溯源: atoms %s%s" % (
            ",".join(str(a) for a in sc["atom_ids"][-5:]),
            "; ledger %s" % ",".join(_xid(x) for x in fixes[-5:]) if fixes else ""))
        lines.append("")
    _atomic_write(SOP, "\n".join(lines))
    return list(scenarios.values())


def find_scenario(signature):
    """查 L2:该失效签名是否已有历史修复。返回 dict 或 None。"""
    if not os.path.exists(SCENARIOS):
        return None
    try:
        for sc in json.load(open(SCENARIOS, encoding="utf-8")):
            if sc.get("signature") == signature:
                return sc
    except Exception:
        pass
    return None


# ---------------- CLI 入口(2026-09-11 审计 P1-7: 记忆层对 AI 会话接线) ----------------
# 任何会话/人工都可经此写入 L1 原子记忆,补上"踩坑→沉淀"闭环的最后一公里:
#   python3 memory.py record <kind> <fact> [ref1,ref2,...]   # 追加原子
#   python3 memory.py rebuild                                 # 重建 L2/L3
#   python3 memory.py find <signature>                        # 查场景

def _cli(argv):
    if len(argv) < 2:
        print("用法: memory.py record <kind> <fact> [refs...] | rebuild | find <sig>")
        return 2
    op = argv[1]
    if op == "record" and len(argv) >= 4:
        refs = [r for r in argv[4:]]
        atom = record_atom(argv[2], argv[3], refs)
        if atom is None:
            print("[memory] 写入被验证拦截(过短/噪音)")
            return 1
        print("[memory] 已记录原子: %s" % atom.get("id"))
        return 0
    if op == "rebuild":
        scs = rebuild()
        print("[memory] 重建完成: %d 个场景" % len(scs))
        return 0
    if op == "find" and len(argv) >= 3:
        sc = find_scenario(argv[2])
        print(json.dumps(sc, ensure_ascii=False, indent=2) if sc else "(无此签名场景)")
        return 0 if sc else 1
    if op == "retract" and len(argv) >= 4:
        ok = retract_atom(argv[2], " ".join(argv[3:]))
        print("[memory] 撤回%s: %s" % ("成功" if ok else "失败(无此 id)", argv[2]))
        return 0 if ok else 1
    if op == "supersede" and len(argv) >= 4:
        ok = supersede_atom(argv[2], argv[3], " ".join(argv[4:]))
        print("[memory] 取代%s: %s → %s" % ("成功" if ok else "失败(无此 id)", argv[2], argv[3]))
        return 0 if ok else 1
    if op == "semantic" and len(argv) >= 3:
        hits = find_scenario_semantic(" ".join(argv[2:]))
        print(json.dumps(hits, ensure_ascii=False, indent=2) if hits else "(无匹配)")
        return 0 if hits else 1
    if op == "current" and len(argv) >= 3:
        a = current_value(argv[2])
        print(json.dumps(a, ensure_ascii=False, indent=2) if a else "(无有效原子)")
        return 0 if a else 1
    print("参数不足或未知操作: %s" % op)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
