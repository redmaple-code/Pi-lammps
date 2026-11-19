#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演化提案应用门(唯一应用通道)
============================
用法:
    python3 apply_proposal.py <proposal_dir> [--approver 名字]   # 应用一份提案
    python3 apply_proposal.py --rollback <ledger_id>             # 回滚一笔已应用提案

提案目录(evolution/proposals/<date>-<slug>/)须含:
    meta.json     {"root": "kit"|"dsh_agent", "class": "...", "tier": "auto"|"human"}
    proposal.md   动机与影响面
    diff.patch    统一 diff(路径相对 root,支持 a/ b/ 前缀、新文件、删文件)
    evidence.md   观测证据(observe.py 可自动生成)

门禁顺序(任一不过即拒,LEDGER 记 REJECTED):
    1. POLICY.json 校验:forbidden_paths 一律拒;auto 类必须在 auto_fix_classes 且
       改动行数 ≤ max_changed_lines 且只许碰该类声明的 paths;human 类须带 --approver
       (经 dsh 弹窗批准后才走到这里,approver 入账)。
    2. 备份受影响文件 → 打补丁(纯 Python 统一 diff 应用器,上下文行严格匹配)。
    3. 跑该 root 的 test_commands,全绿才保留;任一红 → 自动还原备份,记 ROLLED_BACK。
    4. 全绿 → 记 LEDGER(APPLIED/AUTO_APPLIED),备份留 evolution/backups/ 供 --rollback。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mcp"))
import paths

POLICY = json.load(open(os.path.join(HERE, "POLICY.json"), encoding="utf-8"))
# 数据根外置(2026-08-29):LEDGER/backups 落在数据根 memory/ 下
LEDGER = os.path.join(paths.memory_dir(), "LEDGER.jsonl")
BACKUPS_BASE = paths.memory_dir()
REQUIRED_FILES = ("meta.json", "proposal.md", "diff.patch", "evidence.md")


def ledger(record):
    record = dict(record)
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def fail(lid, slug, reason):
    ledger({"id": lid, "slug": slug, "result": "REJECTED", "reason": reason})
    print("[APPLY] REJECTED: %s" % reason)
    return 2


# 测试猴子补丁重定向;None=按 POLICY roots.kit 解析
KIT_GATE_INBOX = None


def _append_gate_resolve(ref, by):
    """批准回执投进 kit 用户闸收件箱(2026-09-07;orchestrator 扫箱销项)。
    任何失败只告警——批准主链永不被桥阻塞。本函数不含任何批准逻辑,
    批准仍只发生在上方人工 --approver 通道之后。"""
    try:
        path = KIT_GATE_INBOX or os.path.join(
            POLICY["roots"]["kit"]["path"], "logs", "user_gate_requests.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"op": "resolve", "ref": ref,
                                "by": by or "unknown", "ts": time.time()},
                               ensure_ascii=False) + "\n")
    except Exception as ex:
        print("[APPLY] warn: 用户闸销项投递失败: %s" % ex, file=sys.stderr)


# ---------- 统一 diff 解析与纯 Python 应用 ----------
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text):
    """解析为 [{'old': path|None, 'new': path|None, 'hunks': [(old_start, lines)]}]。"""
    files = []
    cur = None
    for line in text.splitlines():
        if line.startswith("--- "):
            if cur:
                files.append(cur)
            path = line[4:].strip()
            cur = {"old": None if path == "/dev/null" else _strip_prefix(path),
                   "new": None, "hunks": []}
        elif line.startswith("+++ "):
            path = line[4:].strip()
            cur["new"] = None if path == "/dev/null" else _strip_prefix(path)
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m:
                raise ValueError(" malformed hunk header: %s" % line)
            cur["hunks"].append({"old_start": int(m.group(1)),
                                 "old_count": int(m.group(2) or 1),
                                 "new_count": int(m.group(4) or 1),
                                 "lines": []})
        elif cur and cur["hunks"] and line[:1] in (" ", "+", "-", "\\"):
            cur["hunks"][-1]["lines"].append(line)
    if cur:
        files.append(cur)
    # 校验 hunk 声明行数与正文一致(防畸形 diff 蒙混)
    for f in files:
        for hunk in f["hunks"]:
            old_n = sum(1 for ln in hunk["lines"] if ln[:1] in (" ", "-"))
            new_n = sum(1 for ln in hunk["lines"] if ln[:1] in (" ", "+"))
            if old_n != hunk["old_count"] or new_n != hunk["new_count"]:
                raise ValueError(
                    " hunk 声明(-%d +%d)与正文(-%d +%d)不符"
                    % (hunk["old_count"], hunk["new_count"], old_n, new_n))
    return files


def _strip_prefix(path):
    # GNU diff -u 头部带 tab+时间戳(a/foo.py\t2026-08-27 ...),先截掉
    path = path.split("\t")[0].strip()
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _contained(root_path, rel):
    """diff 路径必须落在 root 内:拒绝绝对路径与 .. 逃逸(含 symlink 解析后)。
    返回规范化后的 root 相对路径(供 POLICY 比对统一口径),越界返回 None。"""
    if os.path.isabs(rel) or ".." in rel.split("/"):
        return None
    real_root = os.path.realpath(root_path)
    real = os.path.realpath(os.path.join(real_root, rel))
    if real != real_root and real.startswith(real_root + os.sep):
        return os.path.relpath(real, real_root)
    return None


def apply_hunks(original, hunks, path):
    lines = list(original)
    offset = 0
    for hunk in hunks:
        idx = hunk["old_start"] - 1 + offset
        if idx < 0:
            idx = 0  # old_start=0 表示"在第 1 行之前"(新文件)
        for entry in hunk["lines"]:
            tag, text = entry[0], entry[1:]
            if tag == "\\":
                continue
            if tag == " ":
                if idx >= len(lines) or lines[idx] != text:
                    raise ValueError("context mismatch in %s near line %d" % (path, idx + 1))
                idx += 1
            elif tag == "-":
                if idx >= len(lines) or lines[idx] != text:
                    raise ValueError("delete mismatch in %s near line %d" % (path, idx + 1))
                del lines[idx]
                offset -= 1
            elif tag == "+":
                lines.insert(idx, text)
                idx += 1
                offset += 1
    return lines


# ---------- POLICY 校验 ----------
def check_policy(meta, changed_files, changed_line_count):
    cls = meta.get("class", "")
    tier = meta.get("tier", "")
    if cls == "evidence_v3_knowledge":
        return "knowledge 类仅允许注册的数据发布分支，禁止通用 diff 写入"
    for path in changed_files:
        if "review_experience" in path.replace("\\", "/").split("/"):
            return "review_experience 必须走受控知识发布分支与共享代次锁"
        for forbidden in POLICY["forbidden_paths"]:
            fb = forbidden.rstrip("/")
            if path == fb or path.startswith(fb + "/"):
                return "触碰红线路径 %s(forbidden_paths)" % path
    if tier == "auto":
        spec = POLICY["auto_fix_classes"].get(cls)
        if spec is None:
            return "class '%s' 不在 auto_fix_classes,Tier1 自动层拒绝" % cls
        if meta.get("root") != spec["root"]:
            return "class '%s' 的 root 应为 '%s'" % (cls, spec["root"])
        allowed = spec["paths"]
        for path in changed_files:
            if not any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed):
                return "auto 类 '%s' 只许改 %s,实际改了 %s" % (cls, allowed, path)
        if changed_line_count > spec["max_changed_lines"]:
            return "改动 %d 行超出 auto 类上界 %d" % (changed_line_count, spec["max_changed_lines"])
    elif tier == "evidence":
        spec = POLICY.get("evidence_classes", {}).get(cls)
        if spec is None:
            return "class '%s' 不在 evidence_classes,证据层拒绝" % cls
        if meta.get("root") != spec["root"]:
            return "class '%s' 的 root 应为 '%s'" % (cls, spec["root"])
        allowed = spec["paths"]
        for path in changed_files:
            if not any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed):
                return "evidence 类 '%s' 只许改 %s,实际改了 %s" % (cls, allowed, path)
        if changed_line_count > spec["max_changed_lines"]:
            return "改动 %d 行超出 evidence 类上界 %d" % (changed_line_count, spec["max_changed_lines"])
    elif tier == "human":
        if cls not in POLICY["human_review_classes"] and cls not in POLICY["auto_fix_classes"] \
                and cls not in POLICY.get("evidence_classes", {}):
            return "class '%s' 未在任何已知类别登记" % cls
    else:
        return "meta.tier 必须是 'auto'、'evidence' 或 'human'"
    return None


def apply_knowledge_proposal(proposal, *, approver, data_root=None):
    """Registered data publication branch of the sole application channel.

    A pending generation is committed before touching knowledge. If any later
    write fails it remains pending, so task acceptance cannot consume a partly
    published package. Recovery requires explicit reconciliation, not retrying
    an unknown write as a new approval.
    """
    from evidence_v3 import knowledge
    from evidence_v3.contracts import V3Error
    from evidence_v3.store import TaskStore, generation_lock
    from pathlib import Path

    try:
        from .kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.publish_knowledge(proposal, approver, data_root)

    if knowledge.CLASS_NAME not in POLICY.get("human_review_classes", []):
        raise V3Error("knowledge-class-not-registered")
    if not isinstance(proposal, dict) or proposal.get("tier") != "human":
        raise V3Error("knowledge-human-tier-required")
    root = Path(data_root or paths.data_root()).resolve()
    store = TaskStore(str(root))
    directory = store._safe(root / "memory" / "review_experience")
    # This is the same lock and projection used by TaskService.start.
    with generation_lock(str(root)):
        generation = store.generation()
        if generation["status"] != "ready":
            raise V3Error("GENERATION_PENDING")
        prepared = knowledge.prepare_publication(proposal, approver, directory)
        return knowledge.apply_prepared(store, prepared, approver)


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2

    if argv[0] == "--rollback":
        return rollback(argv[1])

    proposal_dir = os.path.abspath(argv[0])
    approver = None
    if "--approver" in argv:
        idx = argv.index("--approver")
        if idx + 1 >= len(argv):
            print("[APPLY] REJECTED: --approver 缺名字")
            return 2
        approver = argv[idx + 1]
    slug = os.path.basename(proposal_dir.rstrip("/"))
    lid = uuid.uuid4().hex[:12]

    for name in REQUIRED_FILES:
        if not os.path.exists(os.path.join(proposal_dir, name)):
            return fail(lid, slug, "提案缺文件: %s" % name)
    try:
        with open(os.path.join(proposal_dir, "meta.json"), encoding="utf-8") as stream:
            meta = json.load(stream)
    except (OSError, ValueError) as ex:
        return fail(lid, slug, "meta.json 无法解析: %s" % ex)
    if meta.get("class") == "evidence_v3_knowledge":
        try:
            proposal = dict(meta)
            payload_path = os.path.join(proposal_dir, "knowledge.json")
            if os.path.exists(payload_path):
                from evidence_v3.contracts import read_json
                payload = read_json(payload_path)
                if not isinstance(payload, dict):
                    raise ValueError("knowledge.json must be an object")
                proposal.update(payload)
                if proposal.get("class") != meta.get("class") or proposal.get("tier") != meta.get("tier"):
                    raise ValueError("knowledge metadata mismatch")
            result = apply_knowledge_proposal(proposal, approver=approver)
            print("[APPLY] APPLIED: knowledge %s generation=%s" %
                  (result["entry"]["id"], result["generation"]))
            return 0
        except Exception as ex:
            return fail(lid, slug, "knowledge application failed: %s" % type(ex).__name__)
    root_name = meta.get("root")
    root = POLICY["roots"].get(root_name)
    if root is None:
        return fail(lid, slug, "未知 root: %s" % root_name)
    root_path = root["path"]

    diff_text = open(os.path.join(proposal_dir, "diff.patch"), encoding="utf-8").read()
    try:
        changed = parse_unified_diff(diff_text)
    except ValueError as ex:
        return fail(lid, slug, "diff 解析失败:%s" % ex)
    if not changed:
        return fail(lid, slug, "diff.patch 为空")
    for f in changed:
        if f["new"] is None and f["old"] is None:
            # 双 /dev/null 畸形条目:无路径可校验,下游 changed_files 出 None 会把门禁打崩(二轮对抗)
            return fail(lid, slug, "diff 含双 /dev/null 的畸形文件条目")
        for rel in {f["new"], f["old"]} - {None}:
            norm = _contained(root_path, rel)
            if norm is None:
                return fail(lid, slug, "diff 路径越界(绝对路径/.. 逃逸): %s" % rel)
            # 写回规范化相对路径:POLICY 比对与实际写入同口径,杜绝 ./、// 等变体绕过
            if f["new"] == rel:
                f["new"] = norm
            if f["old"] == rel:
                f["old"] = norm
    for f in changed:
        if f["old"] is None and os.path.exists(os.path.join(root_path, f["new"])):
            # diff 声称新建但文件已存在:不备份就覆盖=原内容不可逆销毁(三轮对抗 B1)
            return fail(lid, slug,
                        "diff 声称新建但文件已存在(防覆盖既有内容): %s" % f["new"])
    changed_files = [f["new"] or f["old"] for f in changed]
    changed_lines = sum(
        1 for f in changed for h in f["hunks"] for ln in h["lines"] if ln[:1] in ("+", "-"))

    if meta.get("tier") == "human" and not approver:
        return fail(lid, slug, "Tier2 人审类必须带 --approver(经 dsh 弹窗批准后再执行)")
    reason = check_policy(meta, changed_files, changed_lines)
    if reason:
        return fail(lid, slug, reason)
    if meta.get("tier") == "auto":
        # auto 内容闸(20 轮对抗):碰策略库的 auto 提案只许动 strategy_adjustments,
        # strategies/history 逐字节不动——此前 auto 类只校验路径/行数,内容可走私。
        reason = _check_auto_db_confinement(changed, root_path)
        if reason:
            return fail(lid, slug, reason)
    if meta.get("class") in POLICY.get("evidence_classes", {}):
        # 证据门(L2):挂 evidence 类的提案无论 tier 一律过门——tier=human+approver
        # 不构成旁路(approver 只入账,不豁免任何证据检查,20 轮对抗)。
        # 纯检查零副作用;不过→REJECTED(未应用,不记 ROLLED_BACK)。
        reason = _check_evidence(meta, changed, root_path)
        if reason:
            return fail(lid, slug, reason)

    # 备份 → 打补丁(backups_dir 相对数据根 memory/ 解析)
    backup_dir = os.path.join(BACKUPS_BASE, POLICY["rollback"]["backups_dir"], "%s_%s" % (lid, slug))
    os.makedirs(backup_dir, exist_ok=True)
    # created 按"打补丁前真实不存在"判定:git 风格手写 diff(--- a/x +++ b/x @@ -0,0 +1 @@)
    # 创建文件时 old 非 None,只按 old is None 判定会漏记 → 回滚被拒、新文件残留(四轮对抗)
    pre_existing = {f["new"] for f in changed
                    if f["new"] and os.path.exists(os.path.join(root_path, f["new"]))}
    try:
        # 两阶段:先整体备份打补丁前的原始状态,再统一打补丁。
        # 备份与打补丁交错时,同文件多条目 diff(先建后改)会把第一条目刚写出的
        # 中间态当"原始"备份,回归红/回滚后文件以中间态残留(五轮对抗)
        for f in changed:
            rel = f["new"] or f["old"]
            src = os.path.join(root_path, rel)
            dst = os.path.join(backup_dir, rel)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
    except (ValueError, OSError) as ex:
        # 备份阶段失败时尚无任何文件被改动,绝不能走 _restore:对无备份的条目
        # _restore 会把未触碰的原文件当"新建"物理删除(六轮对抗)
        shutil.rmtree(backup_dir, ignore_errors=True)
        return fail(lid, slug, "备份失败(未做任何改动):%s" % ex)
    try:
        for f in changed:
            _apply_one(root_path, f)
    except (ValueError, OSError) as ex:
        _restore(root_path, backup_dir, changed)
        shutil.rmtree(backup_dir, ignore_errors=True)
        return fail(lid, slug, "打补丁失败(已还原):%s" % ex)

    if meta.get("class") in POLICY.get("evidence_classes", {}):
        # 落盘后复核(20 轮对抗 simulate/apply TOCTOU):模拟验证读的是打补丁前的盘,
        # 备份到落盘之间 kit 结账户可能写盘且上下文行恰好仍匹配——按真实两端
        # (备份=旧,落盘=新)重放证据断言;不过 → 还原 + ROLLED_BACK(补丁已落盘,
        # 不是 REJECTED)。沙盒试跑确定性且已过一次,不重复跑。
        reason = _recheck_evidence_post_apply(meta, root_path, backup_dir)
        if reason:
            _restore(root_path, backup_dir, changed)
            shutil.rmtree(backup_dir, ignore_errors=True)
            ledger({"id": lid, "slug": slug, "root": root_name, "class": meta.get("class"),
                    "tier": meta.get("tier"), "files": changed_files,
                    "op": meta.get("op"), "specialist_id": meta.get("specialist_id"),
                    "result": "ROLLED_BACK", "reason": "落盘后证据复核未过: %s" % reason})
            print("[APPLY] ROLLED_BACK: 落盘后证据复核未过(%s),已还原" % reason)
            return 3

    # 回归门:任一红 → 还原
    tests_ok, test_log = _run_tests(root_path, root["test_commands"])
    try:
        # 留证失败(提案目录只读/磁盘满)绝不能阻断后续还原与入账——
        # 否则补丁已应用却零入账零还原(七轮对抗)
        open(os.path.join(proposal_dir, "test-results.txt"), "w", encoding="utf-8").write(test_log)
    except OSError as ex:
        print("[APPLY] warn: test-results.txt 写入失败(不影响还原/入账): %s" % ex)
    if not tests_ok:
        _restore(root_path, backup_dir, changed)
        shutil.rmtree(backup_dir, ignore_errors=True)
        ledger({"id": lid, "slug": slug, "root": root_name, "class": meta.get("class"),
                "tier": meta.get("tier"), "files": changed_files,
                "op": meta.get("op"), "specialist_id": meta.get("specialist_id"),
                "result": "ROLLED_BACK", "reason": "回归测试未全绿"})
        print("[APPLY] ROLLED_BACK: 回归测试未全绿,已还原")
        return 3

    result = "AUTO_APPLIED" if meta.get("tier") in ("auto", "evidence") else "APPLIED"
    ledger({"id": lid, "slug": slug, "root": root_name, "class": meta.get("class"),
            "tier": meta.get("tier"), "files": changed_files,
            "op": meta.get("op"), "specialist_id": meta.get("specialist_id"),
            "created": list(dict.fromkeys(
                f["new"] for f in changed
                if f["new"] and f["new"] not in pre_existing)),  # 同文件多条目去重(五轮对抗)
            "changed_lines": changed_lines, "approver": approver,
            "backup": os.path.relpath(backup_dir, BACKUPS_BASE), "result": result})
    print("[APPLY] %s: %s (%s, %d 行, 回归全绿)" % (result, slug, root_name, changed_lines))
    # 对应 human_review 用户闸销项(无此闸则 no-op);仅 human tier 投桥——
    # auto/evidence 从未入队,投 resolve 只是收件箱噪音(24 轮审计)
    if meta.get("tier") == "human":
        _append_gate_resolve(slug, approver)

    # E1(2026-09-14): 进化→记忆同步——变更必须记录, 否则知识库与现实脱节;
    # 同步失败绝不阻断补丁主流程(已入账已还原的路径不容回退)
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        from memory import record_atom, rebuild
        record_atom(
            kind="evolution-change-%s" % (meta.get("class") or "unknown"),
            fact="变更: %s @%s 改 %s (%d 行, %s, approver=%s)" % (
                meta.get("op") or "patch", root_name,
                ",".join(changed_files[:3]), changed_lines, result, approver),
            refs=["ledger:%s" % lid, "proposal:%s" % slug],
        )
        rebuild()  # E3: 自动刷新 SOP(L2/L3 视图跟上现实)
        print("[APPLY] 进化变更已记录到知识库并触发 SOP 重建")
    except Exception as e:
        print("[APPLY] warn: 进化-记忆同步失败(不阻断补丁): %s" % e)

    # E2(2026-09-14): 标记受影响的旧知识为过时(append-only, 不改原子本体,
    # 只新增一条 supersession 记录; 汇总一条防原子爆炸)
    try:
        from memory import load_atoms
        _prefix = ("evolution-change-%s" % (meta.get("class") or "")).rsplit("-", 1)[0]
        for _atom in load_atoms():
            if str(_atom.get("kind", "")).startswith(_prefix):
                from memory import record_atom as _ra
                _ra(kind="knowledge-supersession",
                    fact="原子 %s 的知识已被本次进化变更(%s)取代" % (_atom.get("id"), slug),
                    refs=[str(_atom.get("id")), "proposal:%s" % slug])
                break
    except Exception:
        pass

    # 接缝5修复(2026-09-15): 进化改了监控/温度程序相关文件后, 通知运行中的
    # monitor daemon 热更新。kit 侧 input json 属 mdorch,user 碰不得——信号走
    # mdsim-data(本侧可写,daemon 可读): daemon 每轮 stat 此文件, mtime 新于
    # 自身启动时间即 re-exec 装新代码(V5 热重载只覆盖 schedule 内容, 不覆盖代码)。
    try:
        _watch = ("monitor", "schedule")
        if any(any(w in str(f) for w in _watch) for f in changed_files):
            _sig = os.path.join(BACKUPS_BASE, ".monitor_reload_signal")
            with open(_sig, "w", encoding="utf-8") as _f:
                _f.write(json.dumps({"ts": time.time(), "slug": slug,
                                     "files": changed_files[:5]}))
            os.chmod(_sig, 0o644)
            print("[APPLY] 监控类变更: 已发 daemon 热更新信号(%s)" % _sig)
    except Exception as e:
        print("[APPLY] warn: daemon 热更新信号失败(不阻断): %s" % e)
    return 0


# ---------- 证据门(Tier1.5,L2 结构自进化) ----------
_SID_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
_EVIDENCE_OPS = ("install", "promote", "demote")
_TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _kit_module(root_path, name):
    """从 root 加载 kit 模块(specialist_rules)。文件不存在/加载失败 → None(fail-closed)。"""
    import importlib.util
    path = os.path.join(root_path, "agents", "runtime", "v3.3", name + ".py")
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _canary_stats(db, sid):
    """按盘上新读的 db 重算金丝雀实账(TOCTOU 纪律:绝不采信提案/元数据里的统计)。
    返回 (settled 总数, 胜, 负, 尾部连败, 最早一笔 ts 或 None);ts 过滤同 kit/observe 口径
    (可解析、非未来 >1h)。"""
    import datetime as _dt
    now = _dt.datetime.now()
    wins = fails = streak = 0
    first_ts = None
    rows = []
    for h in db.get("history", []):
        if not isinstance(h, dict) or h.get("strategy") != sid \
                or h.get("outcome") not in ("success", "fail"):
            continue
        try:
            t = _dt.datetime.strptime(str(h.get("ts", "")), _TS_FMT)
        except (TypeError, ValueError):
            continue
        if t > now + _dt.timedelta(hours=1):
            continue
        rows.append((t, h["outcome"]))
    rows.sort(key=lambda x: x[0])
    for t, o in rows:
        if first_ts is None:
            first_ts = t
        if o == "success":
            wins += 1
        else:
            fails += 1
    for _, o in reversed(rows):
        if o != "fail":
            break
        streak += 1
    return wins + fails, wins, fails, streak, first_ts


def _simulate_db_patch(root_path, changed):
    """内存模拟打补丁:返回 (old_db, new_db) 或 (None, 错误串)。
    证据类只许单文件且恰为策略库。"""
    dbs = [f for f in changed
           if (f["new"] or f["old"]) == ".v3.3_strategy_db.json" and f["new"] and f["old"]]
    if len(changed) != 1 or len(dbs) != 1:
        return None, "evidence 类只许单文件修改 .v3.3_strategy_db.json"
    target = os.path.join(root_path, ".v3.3_strategy_db.json")
    try:
        with open(target, encoding="utf-8") as fh:
            old_text = fh.read()
        new_lines = apply_hunks(old_text.splitlines(), changed[0]["hunks"], "db")
        old_db = json.loads(old_text)
        new_db = json.loads("\n".join(new_lines) + "\n")
    except (OSError, ValueError) as ex:
        return None, "策略库读取/模拟打补丁失败: %s" % ex
    if not isinstance(old_db, dict) or not isinstance(new_db, dict):
        return None, "策略库顶层必须是对象"
    return (old_db, new_db), None


def _canon(v):
    """JSON 规范化文本:True≠1、键序无关、NaN 稳定(21 轮对抗:dict== 下
    True==1 穿透"逐字节"承诺;0.5/0.50 语义等价,JSON 层面本就无法区分,不算泄漏)。"""
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _check_auto_db_confinement(changed, root_path):
    """auto 类碰策略库的内容闸(20 轮对抗):只许动 strategy_adjustments;
    strategies/history 逐字节不动(单写者纪律,auto 通道与 evidence 同权)。
    21 轮加固:顶层键集合也不许增删(防顺手夹带 backdoor 顶层键)。"""
    if not any((f["new"] or f["old"]) == ".v3.3_strategy_db.json" for f in changed):
        return None
    res, err = _simulate_db_patch(root_path, changed)
    if err:
        return err
    old_db, new_db = res
    if not isinstance(old_db, dict) or not isinstance(new_db, dict):
        return "策略库顶层必须是对象"
    # 21 轮对抗:顶层键不许增删,唯一豁免=strategy_adjustments 尚不存在时创建它
    # (那本身就是 auto 通道的合法写入面;老库可能无此键)
    if set(old_db) - set(new_db) or set(new_db) - set(old_db) - {"strategy_adjustments"}:
        return "auto 提案不得增删策略库顶层键(%s → %s)" % (sorted(old_db), sorted(new_db))
    for k in old_db:
        if k == "strategy_adjustments":
            continue
        if _canon(old_db[k]) != _canon(new_db[k]):
            return "auto 提案不得改 %s(单写者纪律,只许动 strategy_adjustments)" % k
    if not isinstance(new_db.get("strategy_adjustments", {}), dict):
        return "strategy_adjustments 必须是对象"
    return None


# 血统比对字段:success_rate/uses 随 kit 结账合法漂移,不参与比对
_LINEAGE_KEYS = ("name", "desc", "for", "canary", "rule", "template", "params")


def _lineage_ok(entry, sid, ev):
    """血统:条目稳定字段必须与模板产物逐字节一致——防 install 后人工/异常篡改
    的条目被 promote 洗白为正式策略,或被 demote 通道"合法"删除(20 轮对抗)。"""
    if not isinstance(entry, dict):
        return False
    tid = entry.get("template")
    if tid not in ev.get("allowed_templates", []):
        return False
    import specialist_templates
    expect = specialist_templates.instantiate(tid, sid, entry.get("params"))
    if expect is None:
        return False
    return all(entry.get(k) == expect.get(k) for k in _LINEAGE_KEYS)


def _canary_install_ts(sid):
    """LEDGER 里该 sid 最早一笔 install AUTO_APPLIED 的 ts(datetime 或 None)。
    僵尸金丝雀(零实账)的饿死锚点:history 无账可查时以安装时刻计龄(20 轮对抗)。"""
    import datetime as _dt
    best = None
    if not os.path.exists(LEDGER):
        return None
    with open(LEDGER, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) \
                    or row.get("result") != "AUTO_APPLIED" \
                    or row.get("op") != "install" \
                    or row.get("specialist_id") != sid:
                continue
            try:
                t = _dt.datetime.strptime(str(row.get("ts", "")), _TS_FMT)
            except (TypeError, ValueError):
                continue
            if t > _dt.datetime.now() + _dt.timedelta(hours=1):
                continue
            if best is None or t < best:
                best = t
    return best


def _install_still_needed(old_db, tid, ev):
    """install 落笔前复核"仍然需要":模板根因至少其一仍耗尽(按盘上快照现算,
    与 observe 起草同函数同口径)。草稿可能搁置数日才应用,起草时的耗尽判定
    不能当作安装时事实(20 轮对抗)。"""
    try:
        import observe
        import specialist_templates
    except ImportError as ex:
        return "observe/specialist_templates 加载失败(fail-closed): %s" % ex
    tpl = specialist_templates.TEMPLATES.get(tid)
    if tpl is None:
        return "模板 %r 不在注册表" % tid
    causes = tpl.get("root_causes") or []
    exhausted = observe._exhausted_causes(
        old_db, old_db.get("history"), old_db.get("strategy_adjustments"),
        ev, specialist_templates)
    if not any(c in exhausted for c in causes):
        return ("安装时根因 %s 已不再耗尽(或已有活金丝雀/未过期墓碑),"
                "install 不再必要" % causes)
    return None


def _evidence_db_assertions(meta, ev, old_db, new_db, root_path):
    """证据门结构+统计断言,对 (old_db, new_db) 重放。_check_evidence(内存模拟)
    与 _recheck_evidence_post_apply(落盘后真实两端)共用,保证模拟验证过什么,
    落盘就必须还是什么(20 轮对抗 TOCTOU)。"""
    op = meta.get("op")
    sid = meta.get("specialist_id")
    if not isinstance(old_db, dict) or not isinstance(new_db, dict):
        return "策略库顶层必须是对象"
    # 21 轮对抗:顶层键集合与三大键以外字段同样锁死——install/promote/demote
    # 只许动 strategies,顺手增删/改写其它顶层键=夹带走私
    if set(new_db) != set(old_db):
        return "evidence 提案不得增删策略库顶层键(%s → %s)" % (sorted(old_db), sorted(new_db))
    for k in old_db:
        if k == "strategies":
            continue
        if _canon(old_db[k]) != _canon(new_db[k]):
            return "evidence 提案不得改 %s(单写者纪律,只许动 strategies)" % k
    old_s = old_db.get("strategies", {})
    new_s = new_db.get("strategies", {})
    if not isinstance(old_s, dict) or not isinstance(new_s, dict):
        return "策略库 strategies 必须是对象"

    if op == "install":
        added = set(new_s) - set(old_s)
        if added != {sid} or set(old_s) - set(new_s):
            return "install 必须恰新增策略 %s(实际增 %s 删 %s)" % (
                sid, sorted(added), sorted(set(old_s) - set(new_s)))
        if any(old_s[k] != new_s[k] for k in old_s):
            return "install 不得改动既有策略条目"
        entry = new_s[sid]
        if entry.get("canary") is not True:
            return "install 的新策略必须 canary: true(直接装正式策略=绕过金丝雀)"
        tid = entry.get("template")
        if tid not in ev.get("allowed_templates", []):
            return "模板 %r 不在 allowed_templates" % tid
        sr = _kit_module(root_path, "specialist_rules")
        if sr is None:
            return "specialist_rules 加载失败(fail-closed)"
        verr = sr.validate_rule(entry.get("rule"))
        if verr:
            return "rule 非法: %s" % verr
        import specialist_templates
        expect = specialist_templates.instantiate(tid, sid, entry.get("params"))
        if expect is None or expect != entry:
            return "策略条目必须 == 模板产物(防手工编造 off-template 规则)"
        err = _install_still_needed(old_db, tid, ev)
        if err:
            return err

    elif op == "promote":
        if set(old_s) != set(new_s) or sid not in old_s:
            return "promote 不得增删策略键"
        diff_keys = [k for k in old_s if old_s[k] != new_s[k]]
        if diff_keys != [sid]:
            return "promote 只许改目标策略(实际改 %s)" % diff_keys
        expect = {k: v for k, v in old_s[sid].items() if k != "canary"}
        if old_s[sid].get("canary") is not True or new_s[sid] != expect:
            return "promote 只允许删除 canary: true 一行"
        if not _lineage_ok(old_s[sid], sid, ev):
            return "promote 血统校验失败(稳定字段须与模板产物一致,防篡改洗白)"
        settled, wins, fails, streak, _ = _canary_stats(old_db, sid)
        posterior = (wins + 1.0) / (wins + fails + 2.0)
        if settled < ev.get("canary_attempts", 4):
            return "金丝雀实账不足: %d <%d(按当前盘重算)" % (settled, ev.get("canary_attempts", 4))
        if wins < ev.get("canary_min_wins", 3):
            return "金丝雀胜场不足: %d <%d" % (wins, ev.get("canary_min_wins", 3))
        if posterior < ev.get("min_posterior", 0.5):
            return "后验胜率不达标: %.3f <%.3f" % (posterior, ev.get("min_posterior", 0.5))
        if streak >= ev.get("demote_fail_streak", 3):
            # 达标统计可能起草后即反转:尾部连败触降级线的策略不得晋升(20 轮对抗)
            return "尾部连败 %d 已达降级线,不得晋升(统计在起草后已反转)" % streak

    elif op == "demote":
        if sid not in old_s:
            return "demote 目标策略 %s 不存在(幽灵降级)" % sid
        if set(new_s) != set(old_s) - {sid}:
            return "demote 必须恰删除策略 %s" % sid
        if any(old_s[k] != new_s[k] for k in new_s):
            return "demote 不得改动其他策略条目"
        if old_s[sid].get("canary") is not True:
            return "demote 只适用于金丝雀策略(已晋升策略的移除走人工)"
        if not _lineage_ok(old_s[sid], sid, ev):
            return "demote 血统校验失败(稳定字段须与模板产物一致)"
        settled, _, _, streak, first_ts = _canary_stats(old_db, sid)
        import datetime as _dt
        # 僵尸金丝雀(零实账)以 LEDGER 安装时刻计龄,防永生 canary(20 轮对抗)
        anchor = first_ts or _canary_install_ts(sid)
        starved = False
        if anchor is not None:
            age = (_dt.datetime.now() - anchor).days
            starved = age >= ev.get("canary_max_age_days", 30) \
                and settled < ev.get("canary_attempts", 4)
        if streak < ev.get("demote_fail_streak", 3) and not starved:
            return "降级条件不成立(按当前盘重算): 连败尾 %d,饿死 %s" % (streak, starved)
    return None


def _check_evidence(meta, changed, root_path):
    """证据门。返回 None=通过;否则拒绝原因。纯检查零副作用。
    三操作共用纪律:history/strategy_adjustments 逐字节不动(单写者);
    统计一律按当前盘上新读重算,不信提案自证。"""
    op = meta.get("op")
    sid = meta.get("specialist_id")
    if op not in _EVIDENCE_OPS:
        return "meta.op 必须是 %s" % list(_EVIDENCE_OPS)
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return "meta.specialist_id 非法(须 ^[A-Z][A-Z0-9_]{0,15}$)"
    cls = meta.get("class", "")
    ev = POLICY["evidence_classes"][cls].get("evidence", {})

    res, err = _simulate_db_patch(root_path, changed)
    if err:
        return err
    old_db, new_db = res
    err = _evidence_db_assertions(meta, ev, old_db, new_db, root_path)
    if err:
        return err
    if op == "install" and ev.get("sandbox_trial"):
        # install 必过沙盒试跑(考题出自模板代码,提案自证不作门输入)
        entry = new_db.get("strategies", {}).get(sid) or {}
        err = _run_sandbox_trial(root_path, entry.get("template"), sid,
                                 entry.get("params"))
        if err:
            return err
    return None


def _recheck_evidence_post_apply(meta, root_path, backup_dir):
    """落盘后证据复核:备份(真实旧库)vs 落盘(真实新库)重放断言。返回 None=通过。"""
    cls = meta.get("class", "")
    ev = POLICY["evidence_classes"][cls].get("evidence", {})
    try:
        with open(os.path.join(backup_dir, ".v3.3_strategy_db.json"),
                  encoding="utf-8") as fh:
            old_db = json.load(fh)
        with open(os.path.join(root_path, ".v3.3_strategy_db.json"),
                  encoding="utf-8") as fh:
            new_db = json.load(fh)
    except (OSError, ValueError) as ex:
        return "复核读取策略库失败: %s" % ex
    if not isinstance(old_db, dict) or not isinstance(new_db, dict):
        return "策略库顶层必须是对象"
    return _evidence_db_assertions(meta, ev, old_db, new_db, root_path)


def _run_sandbox_trial(root_path, template_id, sid, params):
    """install 必过:子进程跑沙盒试跑(timeout 180s)。考题出自模板代码;
    提案里的 sandbox_report.json 只是审计佐证,绝不作门输入。"""
    import tempfile
    spec_fd, spec_path = tempfile.mkstemp(suffix=".json", prefix="evidence_spec_")
    try:
        with os.fdopen(spec_fd, "w", encoding="utf-8") as f:
            json.dump({"template": template_id, "sid": sid, "params": params or {}}, f)
        env = dict(os.environ)
        env["KIT_V33"] = os.path.join(root_path, "agents", "runtime", "v3.3")
        r = subprocess.run(
            ["python3.11", os.path.join(HERE, "specialist_sandbox.py"),
             "--trial", spec_path],
            capture_output=True, text=True, timeout=180, env=env)
    except subprocess.TimeoutExpired:
        return "沙盒试跑超时(180s)——ReDoS 或死循环嫌疑,fail-closed"
    except OSError as ex:
        return "沙盒试跑无法执行: %s" % ex
    finally:
        try:
            os.remove(spec_path)
        except OSError:
            pass
    if r.returncode != 0:
        tail = (r.stdout or "").strip().splitlines()
        return "沙盒试跑未过: %s" % (tail[-1][:300] if tail else "无输出")
    return None


def _apply_one(root_path, f):
    rel = f["new"] or f["old"]
    target = os.path.join(root_path, rel)
    if f["new"] is None:  # 删文件
        if os.path.exists(target):
            os.remove(target)
        return
    original = []
    if f["old"] is not None and os.path.exists(target):
        with open(target, encoding="utf-8") as fh:
            original = fh.read().splitlines()
    new_lines = apply_hunks(original, f["hunks"], rel)
    os.makedirs(os.path.dirname(target) or root_path, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("\n".join(new_lines) + "\n")


def _restore(root_path, backup_dir, changed):
    for f in changed:
        rel = f["new"] or f["old"]
        target = os.path.join(root_path, rel)
        backup = os.path.join(backup_dir, rel)
        if os.path.exists(backup):
            os.makedirs(os.path.dirname(target) or root_path, exist_ok=True)
            shutil.copy2(backup, target)
        elif os.path.exists(target):
            os.remove(target)
            # 清理打补丁时 makedirs 建出的空父目录,还原后不留空壳(六轮对抗)
            parent = os.path.dirname(target)
            root_abs = os.path.abspath(root_path)
            while parent and os.path.abspath(parent) != root_abs:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)


def _run_tests(root_path, commands):
    logs = []
    ok = True
    for cmd in commands:
        try:
            r = subprocess.run(cmd, cwd=root_path, capture_output=True, text=True, timeout=600)
            logs.append("$ %s\n%s%s" % (" ".join(cmd), r.stdout, r.stderr))
            if r.returncode != 0:
                ok = False
                break
        except subprocess.TimeoutExpired:
            logs.append("$ %s\nTIMEOUT(600s)" % " ".join(cmd))
            ok = False
            break
        except OSError as ex:
            # 回归命令不存在/不可执行:按红处理(fail-closed),绝不留补丁
            logs.append("$ %s\nEXEC_ERROR: %s" % (" ".join(cmd), ex))
            ok = False
            break
    return ok, "\n\n".join(logs)


def rollback(lid):
    entries = []
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # 坏行跳过,不崩溃
                if isinstance(row, dict):
                    entries.append(row)  # 合法 JSON 非 dict(如 123)同样击穿 .get(四轮对抗)
    hit = next((e for e in reversed(entries)
                if e.get("id") == lid and e.get("result") in ("APPLIED", "AUTO_APPLIED")), None)
    already = any(e.get("id") == lid and e.get("result") == "ROLLED_BACK" for e in entries)
    if hit is None:
        print("[ROLLBACK] 未找到可回滚的 ledger 记录: %s" % lid)
        return 2
    if already:
        print("[ROLLBACK] 该记录已回滚过,拒绝重复回滚: %s" % lid)
        return 2
    # L2-R6 防护(2026-09-03 用户拍板):整文件还原策略库会静默抹掉 kit 结账户
    # 自 apply 以来写入的全部学习历史。>24h 的 db 条目拒绝整文件回滚,
    # 请改用正向修正提案(demote/strategy_weight_tuning);ts 解析失败按超时处理(fail-closed)。
    if ".v3.3_strategy_db.json" in (hit.get("files") or []):
        try:
            age = time.time() - time.mktime(time.strptime(str(hit.get("ts", "")), "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError, OverflowError):
            age = 10 ** 9
        if age < 0:
            # 21 轮对抗:ts 在未来(时钟回拨/伪造)时负年龄绕过 24h 闸——fail-closed 同拒
            print("[ROLLBACK] 拒绝:ledger 记录 ts 在未来(时钟回拨或伪造),"
                  "无法确认条目年龄;请改用正向修正提案")
            return 2
        if age > 24 * 3600:
            print("[ROLLBACK] 拒绝:策略库条目已存在 %.1f 天,整文件还原会抹掉后续学习历史;"
                  "请改用正向修正提案" % (age / 86400.0))
            return 2
    # LEDGER 是潜在被篡改面:回滚目标与备份目录都必须过 containment,防伪造记录覆写 root 外文件
    root_entry = POLICY["roots"].get(hit.get("root"))
    if root_entry is None:
        print("[ROLLBACK] 记录指向未知 root: %s" % hit.get("root"))
        return 2
    root_path = root_entry["path"]
    files = hit.get("files")
    if not isinstance(files, list) or not files:
        print("[ROLLBACK] 记录 files 字段非法")
        return 2
    if any(isinstance(p, str) and "review_experience" in p.replace(chr(92), "/").split("/") for p in files):
        print("[ROLLBACK] 知识包禁止整文件回滚;请通过受控知识撤回/替代提案保留代次与历史")
        return 2
    for p in files:
        if not isinstance(p, str) or _contained(root_path, p) is None:
            print("[ROLLBACK] 记录含越界文件路径,拒绝执行: %r" % p)
            return 2
    backup_rel = hit.get("backup", "")
    real_backups = os.path.realpath(BACKUPS_BASE)
    backup_dir = os.path.realpath(os.path.join(real_backups, backup_rel))
    if backup_dir != real_backups and not backup_dir.startswith(real_backups + os.sep):
        print("[ROLLBACK] 记录 backup 路径越界,拒绝执行: %r" % backup_rel)
        return 2
    if not os.path.isdir(backup_dir):
        # 备份已被清理(如 keep_last 滚动删除):绝不能按"无备份"语义删 root 内文件(二轮对抗)
        print("[ROLLBACK] 备份目录不存在(可能已被清理),拒绝回滚: %s" % backup_rel)
        return 2
    for p in files:
        if os.path.exists(os.path.join(backup_dir, p)):
            continue
        # 备份无此文件:仅当 ledger 明示它是"新建文件"(回滚语义=删除目标)才放行;
        # 否则按备份丢失处理,拒绝执行(绝不把备份丢失变成删文件,二轮对抗)
        created = hit.get("created")
        created = created if isinstance(created, list) else []  # 字符串等畸形值防子串绕过(三轮)
        if p not in created:
            print("[ROLLBACK] 备份缺文件 %s 且非新建文件,拒绝回滚" % p)
            return 2
    changed = [{"new": p, "old": p} for p in files]
    _restore(root_path, backup_dir, changed)
    tests_ok, _ = _run_tests(root_path, POLICY["roots"][hit["root"]]["test_commands"])
    ledger({"id": lid, "slug": hit.get("slug"), "result": "ROLLED_BACK",
            "reason": "手动回滚", "tests_ok_after_rollback": tests_ok})
    print("[ROLLBACK] 已还原 %s(回归%s)" % (hit.get("slug"), "全绿" if tests_ok else "未过,请人工检查"))
    return 0 if tests_ok else 3


if __name__ == "__main__":
    sys.exit(main())
