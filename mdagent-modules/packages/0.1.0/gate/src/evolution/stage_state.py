# -*- coding: utf-8 -*-
"""
stage_state —— 任务级阶段状态机读写器（证据系统 P0）
================================================================================
定位: <数据根>/memory/tasks/<case_id>/ 状态包的唯一读写器。谁推进谁举证:一切阶段
流转必须持 decision 工件路径;锁定决策槽位只追加修订不静默改;审查账/闸口账只追加。

纪律(开发手册 §3.3):
- 纯 stdlib、零 LLM;写入在每任务文件锁内"重读-改-写"(POSIX flock / Windows msvcrt)。
- state.json 整写走 tmp+os.replace;损坏 JSON → 备份 corrupt 副本后拒绝写(TS-004),
  绝不覆盖、绝不丢弃。
- 决策工件 O_EXCL 写入,同名拒绝(不可变)。
- 段集合是数据(memory/stage_sets.json),内置 DEFAULT_STAGE_SET 仅兜底;核心代码不含
  任何域规则字面量。
- on_gate_terminal 为 P1 终态钩子占位(P1 接 record_atom;record_atom 异常不阻断主流程)。
"""
import contextlib
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import fcntl
except ImportError:  # Windows 开发/测试机:msvcrt 兜底;live(Linux)恒走 fcntl
    fcntl = None
    import msvcrt

SCHEMA_VERSION = 1
DEFAULT_STAGE_SET = "default@1"
DEFAULT_STAGES = ["PLAN", "BUILD", "CONFIG", "EXEC", "ANALYZE"]
# 出段闸:离开该段前必须过的闸口(P0 兜底口径,与设计 §5.1.1 一致)
DEFAULT_EXIT_GATES = {"PLAN": "G-PLAN", "BUILD": "G-BUILD", "CONFIG": "G-CONFIG",
                      "EXEC": "G-EXEC", "ANALYZE": "G-ANALYZE"}
RETRY_LIMIT = 3          # 同段连续 HOLD 熔断阈值(R8)
BACKUP_KEEP = 10


class TaskStateError(Exception):
    """非法状态机操作。args[0] 为稳定折词典(TS-001~TS-004),测试可断言。"""


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _data_root(data_root=None):
    if data_root:
        return os.path.abspath(data_root)
    import paths  # mcp/paths.py:env MDSIM_DATA_ROOT 动态解析,测试可重定向
    return paths.data_root()


def tasks_root(data_root=None):
    return os.path.join(_data_root(data_root), "memory", "tasks")


def task_dir(case_id, data_root=None):
    return os.path.join(tasks_root(data_root), case_id)


def state_path(case_id, data_root=None):
    return os.path.join(task_dir(case_id, data_root), "state.json")


# ---------- 段集合注册表(数据优先,内置兜底) ----------
def load_stage_sets(data_root=None):
    fp = os.path.join(_data_root(data_root), "memory", "stage_sets.json")
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                doc = json.load(f)
            sets = doc.get("sets")
            if isinstance(sets, dict) and sets:
                return sets
        except Exception:
            pass  # 坏文件 → 兜底内置,不打崩状态机
    return {DEFAULT_STAGE_SET: {"stages": list(DEFAULT_STAGES),
                                "exit_gates": dict(DEFAULT_EXIT_GATES)}}


def resolve_stage_set(name, data_root=None):
    sets = load_stage_sets(data_root)
    if name not in sets:
        raise TaskStateError("TS-001:unknown-stage-set:%s" % name)
    spec = sets[name]
    stages = spec.get("stages")
    if not isinstance(stages, list) or not stages:
        raise TaskStateError("TS-001:bad-stage-set:%s" % name)
    return spec


# ---------- 可移植排它锁 ----------
@contextlib.contextmanager
def _exclusive_lock(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a") as lk:
        if fcntl is not None:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        else:
            msvcrt.locking(lk.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
            else:
                try:
                    msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass


def _strict_loads(text):
    return json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError("strict-json:%s" % c)))  # 拒 NaN/Infinity,账本文件对 jq 永久损坏教训


def _read_state_unlocked(case_id, data_root=None):
    fp = state_path(case_id, data_root)
    if not os.path.exists(fp):
        return None
    with open(fp, "r", encoding="utf-8") as f:
        try:
            return _strict_loads(f.read())
        except Exception:
            raise TaskStateError("TS-004:state-corrupt:%s" % case_id)


def _backup_corrupt(case_id, data_root=None):
    d = task_dir(case_id, data_root)
    src = os.path.join(d, "state.json")
    dst = os.path.join(d, "backups", "state.corrupt-%s.json" % uuid.uuid4().hex[:8])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        with open(src, "rb") as f, open(dst, "xb") as g:  # O_EXCL,不依赖 umask
            g.write(f.read())
    except FileExistsError:
        pass
    return dst


def _atomic_write_json(fp, doc):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = "%s.tmp.%d.%s" % (fp, os.getpid(), uuid.uuid4().hex[:6])
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, fp)


def _backup_state(case_id, data_root=None):
    d = os.path.join(task_dir(case_id, data_root), "backups")
    os.makedirs(d, exist_ok=True)
    src = state_path(case_id, data_root)
    if not os.path.exists(src):
        return
    dst = os.path.join(d, "state-%s.json" % uuid.uuid4().hex[:8])
    try:
        with open(src, "rb") as f, open(dst, "xb") as g:
            g.write(f.read())
        olds = sorted(x for x in os.listdir(d)
                      if x.startswith("state-") and x.endswith(".json"))
        for extra in olds[:-BACKUP_KEEP]:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, extra))
    except FileExistsError:
        pass


# ---------- 公开接口 ----------
def init_task(case_id, risk_level="medium", stage_set=DEFAULT_STAGE_SET,
              data_root=None, mode="semi", scale_gate="none"):
    """创建任务状态包(幂等:已存在原样返回并标 already_initialized)。"""
    if not case_id or "/" in case_id or "\\" in case_id or case_id.startswith("."):
        raise TaskStateError("TS-001:bad-case-id")
    spec = resolve_stage_set(stage_set, data_root)
    with _exclusive_lock(os.path.join(task_dir(case_id, data_root), "state.lock")):
        existing = None
        try:
            existing = _read_state_unlocked(case_id, data_root)
        except TaskStateError:
            raise  # 已存在但损坏:不允许"重新初始化"覆盖,走 TS-004 人工恢复
        if existing is not None:
            return dict(existing, already_initialized=True)
        d = task_dir(case_id, data_root)
        for sub in ("decisions", "packets", "backups"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        for name in ("review-log.jsonl", "gate-log.jsonl"):
            open(os.path.join(d, name), "a").close()
        for name in ("open-issues.md", "work-log.md"):
            if not os.path.exists(os.path.join(d, name)):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write("# %s\n" % name.replace(".md", "").replace("-", " ").upper()
                            + "\n(由 stage_state.init_task 创建,追加式维护)\n")
        state = {
            "case_id": case_id, "schema_version": SCHEMA_VERSION,
            "stage_set": stage_set, "current_stage": spec["stages"][0],
            "status": "in_progress", "risk_level": risk_level,
            "mode": mode, "scale_gate": scale_gate,
            "started_at": _now_iso(),
            "last_transition": {"ts": _now_iso(), "from": None,
                                "to": spec["stages"][0], "decision_ref": None},
            "revises_count": {s: 0 for s in spec["stages"]},
            "locked_decisions": [], "open_issues": [], "history": [],
            "rollback_of": None,
        }
        _atomic_write_json(state_path(case_id, data_root), state)
        return state


def read_state(case_id, data_root=None):
    """无状态包 → None(最小创建原则:门禁未启用);损坏 → TS-004。"""
    fp = state_path(case_id, data_root)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return _strict_loads(f.read())
    except TaskStateError:
        raise
    except Exception:
        raise TaskStateError("TS-004:state-corrupt:%s" % case_id)


def _check_decision_ref(case_id, decision_ref, data_root=None):
    if not decision_ref:
        raise TaskStateError("TS-002:missing-decision-ref")
    fp = os.path.join(task_dir(case_id, data_root), decision_ref)
    if not os.path.isfile(fp):
        raise TaskStateError("TS-002:decision-ref-not-found:%s" % decision_ref)
    try:
        with open(fp, "r", encoding="utf-8") as f:
            _strict_loads(f.read())
    except Exception:
        raise TaskStateError("TS-002:decision-ref-not-json:%s" % decision_ref)


def transition(case_id, to_stage, decision_ref, data_root=None, via_rollback=False):
    """阶段流转。前置:目标段合法、decision 工件存在且 JSON 合法、只能走后继段
    (回滚路径除外,且需 via_rollback 显式声明)。目标段 revises 清零。"""
    with _exclusive_lock(os.path.join(task_dir(case_id, data_root), "state.lock")):
        try:
            state = _read_state_unlocked(case_id, data_root)
        except TaskStateError as e:
            _backup_corrupt(case_id, data_root)
            raise
        if state is None:
            raise TaskStateError("TS-001:no-state-package:%s" % case_id)
        spec = resolve_stage_set(state.get("stage_set", DEFAULT_STAGE_SET), data_root)
        stages = spec["stages"]
        if to_stage not in stages:
            raise TaskStateError("TS-001:unknown-stage:%s" % to_stage)
        _check_decision_ref(case_id, decision_ref, data_root)
        cur = state["current_stage"]
        if not via_rollback:
            if stages.index(to_stage) != stages.index(cur) + 1:
                raise TaskStateError("TS-003:skip-transition:%s->%s" % (cur, to_stage))
        state["revises_count"][to_stage] = 0
        state["current_stage"] = to_stage
        state["last_transition"] = {"ts": _now_iso(), "from": cur, "to": to_stage,
                                    "decision_ref": decision_ref}
        state["history"].append({"ts": _now_iso(), "stage": cur,
                                 "decision": "advanced",
                                 "artifacts": [decision_ref],
                                 "rollback": bool(via_rollback)})
        if via_rollback:
            state["status"] = "rolled_back"
            state["rollback_of"] = cur
        _backup_state(case_id, data_root)
        _atomic_write_json(state_path(case_id, data_root), state)
        return state


def complete_task(case_id, decision_ref, data_root=None):
    """末段 ANALYZE 出段后收口:status=completed,history 记一笔。"""
    with _exclusive_lock(os.path.join(task_dir(case_id, data_root), "state.lock")):
        state = _read_state_unlocked(case_id, data_root)
        if state is None:
            raise TaskStateError("TS-001:no-state-package:%s" % case_id)
        _check_decision_ref(case_id, decision_ref, data_root)
        state["status"] = "completed"
        state["history"].append({"ts": _now_iso(), "stage": state["current_stage"],
                                 "decision": "completed",
                                 "artifacts": [decision_ref], "rollback": False})
        _backup_state(case_id, data_root)
        _atomic_write_json(state_path(case_id, data_root), state)
        return state


def bump_revises(case_id, stage, data_root=None):
    """HOLD 时 +1(R8 读它)。达到熔断阈值时落 review-loop-block 原子(P1 起生效)。"""
    with _exclusive_lock(os.path.join(task_dir(case_id, data_root), "state.lock")):
        state = _read_state_unlocked(case_id, data_root)
        if state is None:
            raise TaskStateError("TS-001:no-state-package:%s" % case_id)
        state["revises_count"][stage] = state["revises_count"].get(stage, 0) + 1
        state["status"] = "in_progress"
        _backup_state(case_id, data_root)
        _atomic_write_json(state_path(case_id, data_root), state)
        n = state["revises_count"][stage]
        if n >= RETRY_LIMIT:
            on_gate_terminal(case_id, stage, "review-loop-exhausted",
                             {"revises": n}, data_root)
        return n


def append_review_log(case_id, row, data_root=None):
    _append_jsonl(os.path.join(task_dir(case_id, data_root), "review-log.jsonl"), row)


def append_gate_log(case_id, row, data_root=None):
    """闸口校验账(L0 性质事件流:只追加、只读消费)。"""
    _append_jsonl(os.path.join(task_dir(case_id, data_root), "gate-log.jsonl"), row)


def _append_jsonl(fp, row):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    line = json.dumps(dict(row, ts=row.get("ts") or _now_iso()),
                      ensure_ascii=False)
    with open(fp, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_gate_log(case_id, n=50, data_root=None):
    fp = os.path.join(task_dir(case_id, data_root), "gate-log.jsonl")
    rows = []
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            for ln in f.readlines()[-max(1, min(n, 1000)):]:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(_strict_loads(ln))  # 严格模式:NaN/Infinity 行跳过
                except Exception:
                    continue
    return rows


def record_decision_artifact(case_id, record, slug="", data_root=None):
    """决策工件 O_EXCL 写入(不可变):同名拒绝,改判=新文件+review-log 补记。"""
    if not isinstance(record, dict):
        raise TaskStateError("TS-002:record-not-dict")
    import uuid
    name = "%s-%s-%s.json" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                              (slug or "decision").replace(" ", "-")[:40],
                              uuid.uuid4().hex[:6])
    fp = os.path.join(task_dir(case_id, data_root), "decisions", name)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    try:
        with open(fp, "x", encoding="utf-8") as f:
            f.write(json.dumps(dict(record, case_id=case_id,
                                    schema_version=SCHEMA_VERSION),
                               ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        raise TaskStateError("TS-002:artifact-exists:%s" % name)
    return os.path.join("decisions", name).replace("\\", "/")


def lock_decisions(case_id, slots, data_root=None, basis=None):
    """初始化/合并锁定决策槽位。槽位是数据;已有槽不覆盖(改走 revise_decision)。
    basis(v2.2): {槽位id: {kind: user|literature|prior_case|physics_estimate,
    ref, value, reason, scope}} —— E1 依据完备的存储位。
    锁定的槽位同时登记进 state.locked_decisions（E1 的检查对象）。"""
    fp = os.path.join(task_dir(case_id, data_root), "decisions.json")
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    doc = {}
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            doc = json.load(f)
    for sid, value in (slots or {}).items():
        if sid not in doc:
            doc[sid] = {"value": value, "locked_at": _now_iso(), "revisions": []}
        if sid in (basis or {}):
            doc[sid]["basis"] = basis[sid]
    _atomic_write_json(fp, doc)
    # 登记锁定槽位（此前 locked_decisions 只初始化从不写入，E1/R17 因此永不生效）
    with _exclusive_lock(os.path.join(task_dir(case_id, data_root), "state.lock")):
        st = _read_state_unlocked(case_id, data_root)
        if isinstance(st, dict):
            locked = list(st.get("locked_decisions") or [])
            for sid in (slots or {}):
                if sid not in locked:
                    locked.append(sid)
            st["locked_decisions"] = locked
            _atomic_write_json(state_path(case_id, data_root), st)
    return doc


def read_decisions(case_id, data_root=None):
    fp = os.path.join(task_dir(case_id, data_root), "decisions.json")
    if not os.path.exists(fp):
        return {}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def revise_decision(case_id, slot_id, reason, review_ref, data_root=None):
    """锁定槽位显式修订:revisions 只追加(决策修订可追溯)。"""
    fp = os.path.join(task_dir(case_id, data_root), "decisions.json")
    with _exclusive_lock(os.path.join(task_dir(case_id, data_root), "state.lock")):
        doc = read_decisions(case_id, data_root)
        if slot_id not in doc:
            raise TaskStateError("TS-001:unknown-decision-slot:%s" % slot_id)
        doc[slot_id].setdefault("revisions", []).append(
            {"ts": _now_iso(), "reason": str(reason)[:400],
             "review_ref": str(review_ref)[:200]})
        _atomic_write_json(fp, doc)
        return doc


# ---------- P1 终态钩子(占位实现见开发手册 §4.4) ----------
def on_gate_terminal(case_id, stage, outcome, detail=None, data_root=None):
    """闸口终态 → 回流 atoms。P0 为 no-op 占位;P1 由 _record_terminal_atom 填充。
    record_atom 异常绝不阻断状态机主流程(与"打标器故障不阻断入账"同纪律)。"""
    try:
        _record_terminal_atom(case_id, stage, outcome, detail or {}, data_root)
    except Exception:
        pass


def _record_terminal_atom(case_id, stage, outcome, detail, data_root):
    kind_map = {"go_then_failed": "stage-decision",
                "blocked_overturned": "gate-rejection",
                "rollback": "stage-rollback",
                "review-loop-exhausted": "review-loop-block"}
    kind = kind_map.get(outcome)
    if not kind:
        return
    fact = "%s %s %s: %s" % (case_id, stage, outcome,
                             json.dumps(detail, ensure_ascii=False)[:300])
    try:
        import memory  # evolution/memory.py:唯一入账通道(Windows 无锁回退可跑)
        memory.record_atom(kind, fact, refs=["gate-log:" + case_id])
    except ImportError:
        pass


# ---------- P2:任务阶段聚合(看板卡/E2E 汇总,只读) ----------
def list_task_summaries(data_root=None):
    """扫 tasks/*/state.json 聚合一屏摘要。分节容错:单任务损坏不影响整表。"""
    root = tasks_root(data_root)
    out = []
    if not os.path.isdir(root):
        return out
    for case_id in sorted(os.listdir(root)):
        try:
            state = read_state(case_id, data_root)
        except TaskStateError:
            state = {"case_id": case_id, "_corrupt": True}
        except Exception:
            continue
        if state is None:
            continue
        revises = state.get("revises_count") or {}
        gate_rows = read_gate_log(case_id, 1, data_root)
        last_gate = gate_rows[-1] if gate_rows else None
        out.append({
            "case_id": case_id,
            "stage": state.get("current_stage"),
            "status": state.get("status"),
            "risk_level": state.get("risk_level"),
            "mode": state.get("mode", "semi"),
            "scale_gate": state.get("scale_gate", "none"),
            "strike": max(revises.values()) if revises else 0,
            "blocked_by_review_loop": any(v >= RETRY_LIMIT for v in revises.values()),
            "last_gate": {"gate": last_gate.get("gate"), "result": last_gate.get("result"),
                          "reasons": last_gate.get("reasons")} if last_gate else None,
            "corrupt": bool(state.get("_corrupt")),
        })
    return out
