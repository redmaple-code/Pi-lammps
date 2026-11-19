#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行为策略·自动蒸馏(2026-09-17 用户拍板:行为复查类提案去人工化)
================================================================
五类行为复发信号(否定纠正/重复追问/打断/工具错误/审批否决)的提案
不再开人工闸排队等点击——点击被实证为"已读回执",不产生行为变更。
改为自动蒸馏:

    观测复发 → 策略写本库(source=auto-distill,带7天基线) → 渲染进 persona
    (agent.cordis.yml + headless patch 两处,程序保证逐字一致,新会话生效)
    → 每轮 observe 后验:窗口计数持续连升(复发仍在发生) → 自动撤回+移出 persona
    → 撤回后才开人工闸叫人(异常才介入)

人工没有消失,降级为周报复核:看板"行为策略"卡可翻案(revoke),
翻案后该签名永久回到人工闸,不再自动蒸馏。

存储: mdsim-data/memory/behavior_strategies.json(整写+文件锁,单写者 observe)
"""
import fcntl
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mcp"))
import paths

STORE = os.path.join(paths.memory_dir(), "behavior_strategies.json")
DSH_AGENT = os.path.dirname(HERE)
PERSONA_FILES = [
    # (路径, 块标量键名)——两处必须逐字一致(原人肉红线,现由本模块程序保证)
    (os.path.join(DSH_AGENT, "presets", "md-sim-assistant",
                  "agent.cordis.yml"), "text"),
    (os.path.join(DSH_AGENT, "md-sim-assistant.headless.patch.yml"), "persona"),
]

MARK_BEGIN = "【行为策略·自动蒸馏·BEGIN(勿手改,由 evolution/behavior_strategies.py 维护)】"
MARK_END = "【行为策略·自动蒸馏·END】"

# 蒸馏模板:v1 确定性规则文本(非 LLM 生成)。措辞可在周报复核里翻案后由人工改。
RULE_TEMPLATES = {
    "user-correction":
        "被用户否定纠正时:先逐字接纳用户给出的事实再给新答案,不辩解不绕弯;"
        "同一事实性错误不得犯第二次,当场把它写进持久记忆。",
    "repeat-question":
        "用户重复追问=上次没答到点:这轮直接给结论/数值/路径,铺垫最多一句;"
        "答完自查'用户最初那个问题,答案在第几句'。",
    "friction-interrupt":
        "长回复分段,每段开头一句结论;用户打断立即停手,先响应新指令,"
        "不续讲被打断的内容,除非用户明确要求继续。",
    "friction-tool-error":
        "调工具前校验参数(路径存在/类型对/必填齐);失败后先读报错再改,"
        "同一工具连错两次必须换方法或问人,不原样第三次。",
    "friction-denial":
        "请求审批前在说清'要做什么+为什么+不做会怎样';被拒一次不原样重发,"
        "换更小的方案或把决定权交回用户。",
}

EVAL_KEEP = 4          # 后验评估窗口:最近 EVAL_KEEP 轮 observe 计数严格递增→撤回
COOLDOWN_SEC = 7 * 86400  # 撤回后冷却期,期内该签名提案回人工闸(防撤回-重提振荡)


def _default_db():
    return {"version": 1, "strategies": [], "revoked_signatures": [],
            "cooled": {}}


def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, dict) or not isinstance(db.get("strategies"), list):
            raise ValueError("bad shape")
        db.setdefault("revoked_signatures", [])
        db.setdefault("cooled", {})
        return db
    except Exception:
        return _default_db()


def _save(db):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    lock_path = STORE + ".lock"
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        tmp = "%s.tmp.%d" % (STORE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STORE)
        fcntl.flock(lk, fcntl.LOCK_UN)


def route(signature):
    """该签名的提案走哪条路:'auto'=自动蒸馏,'human'=人工闸。
    翻案过/冷却期内的签名回人工闸(异常才介入,防振荡)。"""
    db = _load()
    if signature in db["revoked_signatures"]:
        return "human"
    cooled = db["cooled"].get(signature, 0)
    if time.time() - cooled < COOLDOWN_SEC:
        return "human"
    if signature in RULE_TEMPLATES:
        return "auto"
    return "human"


def _find_active(db, signature):
    for s in db["strategies"]:
        if s.get("signature") == signature and s.get("status") == "active":
            return s
    return None


def auto_distill(signature, title, pdir, baseline_count):
    """提案落草稿后调用:蒸馏为 active 策略(同签名幂等——已 active 只追证据),
    并重渲染 persona 两处。返回策略 dict 或 None。"""
    if signature not in RULE_TEMPLATES:
        return None
    db = _load()
    s = _find_active(db, signature)
    if s:
        s.setdefault("evidence_refs", [])
        if pdir and pdir not in s["evidence_refs"]:
            s["evidence_refs"].append(pdir)
        _save(db)
        return s
    sid = "bs-%s-%s" % (time.strftime("%Y%m%d"), signature)
    s = {"sid": sid, "signature": signature, "title": title,
         "rule": RULE_TEMPLATES[signature], "source": "auto-distill",
         "status": "active", "created_ts": time.time(),
         "baseline_7d": int(baseline_count or 0),
         "evals": [], "evidence_refs": [pdir] if pdir else []}
    db["strategies"].append(s)
    _save(db)
    render_persona(db)
    return s


def verify(counts, enqueue_gate=None):
    """每轮 observe 末尾调用。counts: {signature: 当前7天窗口计数}。
    判定"策略没起作用"=窗口计数仍在严格增长(新事件持续发生);
    计数持平/下降说明没有新增复发,不撤回(7天窗的老事件要7天才衰减,
    拿窗计数对基线必误判——蒸馏时基线即当前值,三轮必误撤,同日实修)。
    active 策略最近 EVAL_KEEP 轮严格递增 → 撤回+移出 persona+冷却;
    撤回开人工闸叫人(此时人工才有信息量)。返回本轮撤回的策略列表。"""
    db = _load()
    changed = False
    reverted = []
    for s in db["strategies"]:
        if s.get("status") != "active":
            continue
        c = counts.get(s["signature"])
        if c is None:
            continue
        s.setdefault("evals", []).append({"ts": time.time(), "count": int(c)})
        s["evals"] = s["evals"][-EVAL_KEEP:]
        changed = True
        ev = s["evals"]
        if len(ev) >= EVAL_KEEP and all(
                ev[i]["count"] > ev[i - 1]["count"] for i in range(1, len(ev))):
            s["status"] = "reverted"
            s["reverted_ts"] = time.time()
            s["revert_reason"] = "策略生效后计数仍连升(基线%s,轮次%s)" % (
                s.get("baseline_7d"), [e["count"] for e in ev])
            db["cooled"][s["signature"]] = time.time()
            reverted.append(s)
    if changed:
        _save(db)
    if reverted:
        render_persona(db)
        if enqueue_gate:
            for s in reverted:
                try:
                    enqueue_gate({
                        "op": "enqueue", "kind": "evolution_proposal",
                        "ref": "auto-strategy-reverted-%s" % s["sid"],
                        "question": "自动策略失效:%s(%s)——需人工复查" % (
                            s["title"], s.get("revert_reason", "")),
                        "evidence_refs": s.get("evidence_refs", []),
                        "source": "dsh"})
                except Exception as e:
                    print("[BSTRAT] warn: 撤回开闸失败: %s" % e, file=sys.stderr)
    return reverted


def revoke(sid, by="dashboard"):
    """人工翻案(看板周报卡):策略撤下+签名永久回人工闸。返回 (ok, msg)。"""
    db = _load()
    for s in db["strategies"]:
        if s.get("sid") == sid:
            if s.get("status") != "active":
                return False, "策略非 active(当前 %s)" % s.get("status")
            s["status"] = "revoked-by-human"
            s["revoked_ts"] = time.time()
            s["revoked_by"] = by
            if s["signature"] not in db["revoked_signatures"]:
                db["revoked_signatures"].append(s["signature"])
            _save(db)
            render_persona(db)
            return True, "ok"
    return False, "未知 sid"


def _render_lines(db):
    rules = [s["rule"] for s in db["strategies"] if s.get("status") == "active"]
    lines = ["", "      " + MARK_BEGIN]
    if rules:
        lines.append("      以下行为策略由演化层自动蒸馏生效(后验不达标自动撤回):")
        for r in rules:
            lines.append("      · " + r)
    else:
        lines.append("      (当前无生效策略)")
    lines.append("      " + MARK_END)
    return lines


def _rewrite_block(path, key, new_section_lines):
    """在 YAML 块标量(key: >-)内幂等重写标记段。返回 True=有改动。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    key_idx = None
    key_indent = 0
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped == "%s: >-" % key or stripped == "%s: |-" % key:
            key_idx = i
            key_indent = len(ln) - len(stripped)
            break
    if key_idx is None:
        raise ValueError("%s: 找不到块标量键 %s" % (path, key))
    # 块范围:键行之后,首个非空且缩进 <= key_indent 的行即块外
    body_start = key_idx + 1
    body_end = len(lines)
    for i in range(body_start, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent <= key_indent:
            body_end = i
            break
    body = lines[body_start:body_end]
    # 去掉旧标记段(若存在)
    out, skipping = [], False
    for ln in body:
        if MARK_BEGIN in ln:
            skipping = True
            continue
        if MARK_END in ln and skipping:
            skipping = False
            continue
        if not skipping:
            out.append(ln)
    # 去掉尾部空行,追加新段
    while out and out[-1].strip() == "":
        out.pop()
    out.extend(new_section_lines)
    new_lines = lines[:body_start] + out + lines[body_end:]
    new_text = "\n".join(new_lines) + "\n"
    old_text = open(path, encoding="utf-8").read()
    if new_text == old_text:
        return False
    bak = path + ".bak_bstrat"
    if not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            f.write(old_text)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


def render_persona(db=None):
    """把 active 规则段同时写进两处 persona(同一批行→逐字一致由程序保证)。
    任何一处失败都告警但不抛——observe 主链不被 persona 重写阻塞。"""
    db = db or _load()
    section = _render_lines(db)
    for path, key in PERSONA_FILES:
        try:
            _rewrite_block(path, key, section)
        except Exception as e:
            print("[BSTRAT] warn: persona 重写失败 %s: %s" % (path, e),
                  file=sys.stderr)


def list_all():
    """看板 API 用:全量策略+翻案签名+冷却表。"""
    db = _load()
    return {"strategies": db["strategies"],
            "revoked_signatures": db["revoked_signatures"],
            "cooled": db["cooled"]}


if __name__ == "__main__":
    # 手动运维: python3 behavior_strategies.py render|list|revoke <sid>
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "render":
        render_persona()
        print("persona 两处已重渲染")
    elif cmd == "revoke" and len(sys.argv) > 2:
        print(revoke(sys.argv[2]))
    else:
        print(json.dumps(list_all(), ensure_ascii=False, indent=1))
