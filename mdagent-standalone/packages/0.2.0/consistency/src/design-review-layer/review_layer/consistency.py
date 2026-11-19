"""Consistency engine: intent fields vs in-file implementation.

Produces ConsistencyItem list with the five-verdict taxonomy:
ok / questionable / unproven / invalid / plan_conflict.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import ConsistencyItem

RULES = Path(__file__).with_name("rules") / "infile_rules.json"


def _rules() -> dict:
    return json.loads(RULES.read_text(encoding="utf-8"))


def _commands(text: str):
    for i, raw in enumerate(text.splitlines(), 1):
        cmd = raw.split("#", 1)[0].strip()
        if cmd:
            yield i, cmd.split()


def _variables(text: str) -> dict:
    """LAMMPS variables visible to later commands (equal/index kept as text)."""
    out = {}
    for _, tok in _commands(text):
        if len(tok) >= 4 and tok[0] == "variable" and tok[2] in ("equal", "index"):
            out[tok[1]] = " ".join(tok[3:])
    return out


def _resolve(expr, variables: dict) -> str:
    for _ in range(12):
        nxt = re.sub(r"\$\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), str(expr))
        if nxt == expr:
            break
        expr = nxt
    return expr


def _number(expr, variables):
    if isinstance(expr, (int, float)) and not isinstance(expr, bool):
        return float(expr)
    text = _resolve(expr, variables).strip()
    if re.fullmatch(r"[0-9eE\s\.\+\-\*/\(\)]+", text or ""):
        try:
            return float(eval(text, {"__builtins__": {}}, {}))
        except Exception:
            return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _integrators(text: str):
    """(line, ensemble, tokens, variables) for each integration fix."""
    variables = _variables(text)
    out = []
    for ln, tok in _commands(text):
        if len(tok) >= 4 and tok[0] == "fix" and tok[3] in ("nvt", "npt", "nve", "nph"):
            out.append((ln, tok[3], tok, variables))
    return out


def _temps(text: str):
    """Temperatures actually set by integration fixes, resolved from variables."""
    out = []
    for ln, ens, tok, variables in _integrators(text):
        if len(tok) >= 6 and tok[4] == "temp":
            t1 = _number(tok[5], variables)
            t2 = t1
            if len(tok) > 6 and tok[6] not in ("iso", "aniso", "x", "y", "z"):
                t2 = _number(tok[6], variables)
            if t1 is not None:
                out.append({"line": ln, "ensemble": ens, "t_start": t1,
                            "t_stop": t1 if t2 is None else t2})
    return out


def _potentials(text: str):
    styles, coeffs = [], []
    for ln, tok in _commands(text):
        if tok[0] == "pair_style":
            styles.append((ln, tok[1:]))
        elif tok[0] == "pair_coeff":
            coeffs.append((ln, tok[1:]))
    return styles, coeffs


_POT_FILE_RE = re.compile(r"\.(eam|meam|alloy|tersoff|sw|lj|morse|table)$", re.I)


def _forcefield_check(intent: dict, text: str):
    """Declared preferred potential vs the potential actually selected in pair_coeff."""
    kp = intent.get("key_params") or {}
    pref = kp.get("forcefield")
    if isinstance(pref, dict):
        pref = pref.get("preferred")
    if not pref:
        return None
    preferred = str(pref).strip()
    styles, coeffs = _potentials(text)
    style_tokens = {t for _, toks in styles for t in toks}
    coeff_text = " ".join(" ".join(toks) for _, toks in coeffs)
    file_tokens = [x for x in re.split(r"[+\s]+", preferred) if _POT_FILE_RE.search(x)]
    if file_tokens:
        missing = [x for x in file_tokens if x not in coeff_text]
        detail = "intent 指定 %s；pair_coeff = %s" % (preferred, coeff_text or "（未设置）")
        if missing:
            return {"verdict": "invalid", "anchors": [ln for ln, _ in coeffs],
                    "detail": detail + "；未找到声明文件 " + "、".join(missing)}
        return {"verdict": "ok", "anchors": [ln for ln, _ in coeffs], "detail": detail}
    wanted = [x for x in re.split(r"[+\s/]+", preferred) if x]
    missing = [x for x in wanted if x not in style_tokens and x not in coeff_text]
    detail = "intent 指定 %s；pair_style = %s" % (preferred, " ".join(sorted(style_tokens)) or "（未设置）")
    if missing:
        return {"verdict": "questionable", "anchors": [ln for ln, _ in styles],
                "detail": detail + "；未找到 " + "、".join(missing)}
    return {"verdict": "ok", "anchors": [ln for ln, _ in styles], "detail": detail}


def _temperature_check(intent: dict, text: str):
    declared = []
    for stage in intent.get("ensemble_sequence") or []:
        if stage.get("T_K") is not None:
            declared.append((str(stage.get("ensemble") or "").lower(), float(stage["T_K"])))
    if not declared:
        return None
    actual = _temps(text)
    if not actual:
        return {"verdict": "unproven", "anchors": [],
                "detail": "intent 声明温度 %s，脚本未找到 nvt/npt 温度设置"
                          % "、".join("%s %gK" % x for x in declared)}
    pairs, missing = [], []
    for ens, temp in declared:
        # NVE has no thermostat, so one NVE stage may legitimately share the same
        # integration fix with a neighbouring NVT stage; only thermostat fixes set T.
        match = [a for a in actual if not ens or a["ensemble"] == ens]
        if not match and ens == "nve" and actual:
            match = [actual[0]]
        if not match:
            missing.append((ens, temp))
        else:
            pairs.append((ens, temp, match[0]))
    bad = [p for p in pairs if p[0] != "nve" and abs(p[2]["t_stop"] - p[1]) > 1e-6]
    anchors = [p[2]["line"] for p in pairs]
    detail = "intent 声明 %s；脚本设置 %s" % (
        "、".join("%s %gK" % (e or "（未指定系综）", t) for e, t in declared),
        "、".join("L%d %s %gK" % (a["line"], a["ensemble"], a["t_stop"]) for a in actual))
    if bad:
        return {"verdict": "invalid", "anchors": anchors, "detail": detail + "；温度不一致"}
    if missing:
        return {"verdict": "questionable", "anchors": anchors,
                "detail": detail + "；声明中的 %s 没有对应系综" % "、".join(e or "?" for e, _ in missing)}
    return {"verdict": "ok", "anchors": anchors, "detail": detail}


def _declared_rate(text: str):
    """Rate in s^-1 from a declared loading description.

    Prefer a value explicitly followed by s⁻¹ / s^-1 (the right-hand side of
    ``... ps⁻¹ = 1×10⁸ s⁻¹``); otherwise fall back to the first scientific value.
    """
    s = str(text or "")
    per_second = r"\s*s\s*(?:⁻¹|\^-?1|-1)"
    m = re.search(r"1(?:\.\d+)?\s*[×xX]\s*10([⁰¹²³⁴⁵⁶⁷⁸⁹\-⁻]+)" + per_second, s)
    if m:
        sup = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
        try:
            return 10.0 ** int(m.group(1).translate(sup))
        except ValueError:
            pass
    m = re.search(r"(?<![\w.])1(?:\.\d+)?[eE]([+-]?\d+)" + per_second, s)
    if m:
        return 10.0 ** int(m.group(1))
    m = re.search(r"1(?:\.\d+)?\s*[×xX]\s*10([⁰¹²³⁴⁵⁶⁷⁸⁹\-⁻]+)", s)
    if m:
        sup = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
        try:
            return 10.0 ** int(m.group(1).translate(sup))
        except ValueError:
            pass
    m = re.search(r"(?<![\w.])1(?:\.\d+)?[eE]([+-]?\d+)", s)
    if m:
        return 10.0 ** int(m.group(1))
    return None


def _loading_check(intent: dict, text: str):
    loading = ((intent.get("cross_scale") or {}).get("loading") or {})
    if not loading:
        return None
    variables = _variables(text)
    deform, moves, velocities = [], [], []
    for ln, tok in _commands(text):
        if len(tok) >= 4 and tok[0] == "fix" and tok[3] == "deform":
            rate = None
            for i, t in enumerate(tok):
                if t == "erate" and i + 1 < len(tok):
                    rate = _number(tok[i + 1], variables)
            deform.append({"line": ln, "axis": tok[4] if len(tok) > 4 else "?", "erate": rate})
        elif len(tok) >= 4 and tok[0] == "fix" and tok[3] == "move":
            moves.append({"line": ln, "group": tok[1], "args": tok[4:]})
        elif tok[0] == "velocity" and len(tok) >= 4 and tok[2] == "set":
            velocities.append({"line": ln, "group": tok[1], "vector": tok[3:6]})
    declared_micro = str(loading.get("micro") or "")
    declared_rate = _declared_rate(declared_micro)
    if deform:
        rates = [d for d in deform if d["erate"] is not None]
        if not rates:
            return {"verdict": "unproven", "anchors": [d["line"] for d in deform],
                    "detail": "声明加载 %s；脚本 fix deform 的 erate 无法解析" % declared_micro[:80]}
        signed = rates[0]["erate"]
        actual = abs(signed) * 1e12  # ps^-1 -> s^-1 (compression is negative by LAMMPS convention)
        detail = "声明 %s；脚本 fix deform %s erate=%g ps^-1 = %.3g s^-1（L%d）" % (
            declared_micro[:90], rates[0]["axis"], signed, actual, rates[0]["line"])
        if declared_rate is None:
            return {"verdict": "questionable", "anchors": [d["line"] for d in deform],
                    "detail": detail + "；声明中无数值速率可供核对"}
        if abs(actual / declared_rate - 1) <= 0.2:
            return {"verdict": "ok", "anchors": [d["line"] for d in deform], "detail": detail}
        return {"verdict": "invalid", "anchors": [d["line"] for d in deform],
                "detail": detail + "；与声明速率差 %.2g 倍" % (actual / declared_rate)}
    if "应力控制" in declared_micro or "stress" in declared_micro.lower():
        stress = [ln for ln, tok in _commands(text)
                  if len(tok) >= 8 and tok[0] == "fix" and tok[3] == "npt"
                  and any(x in tok for x in ("x", "y", "z"))]
        if stress:
            return {"verdict": "ok", "anchors": stress,
                    "detail": "声明应力控制；脚本 NPT 压力控制（L%s），机制一致"
                              % ",".join(map(str, stress))}
        return {"verdict": "questionable", "anchors": [],
                "detail": "声明应力控制；脚本未找到 NPT 压力控制"}
    if moves:
        lines = [m["line"] for m in moves]
        # A declared velocity formula usually lives in a LAMMPS variable; if the
        # actual fix move hard-codes a different value, the declared derivation is
        # not the one being used.
        declared_var = None
        for name, expr in variables.items():
            if name and (name in declared_micro or re.search(r"velocity|speed|rate", name, re.I)):
                declared_var = (name, expr)
                break
        used = []
        for m in moves:
            used.extend(m["args"])
        if declared_var and not any(declared_var[0] in str(x) or ("v_" + declared_var[0]) in str(x)
                                    for x in used):
            return {"verdict": "questionable", "anchors": lines,
                    "detail": "声明以 %s（%s）推导加载速度，但脚本 fix move 使用固定值 %s（L%s），"
                              "未引用该变量；除非两者数值恰好相等，否则实现与声明不一致，需核对"
                              % (declared_var[0], declared_var[1][:60], " ".join(used[:4]) or "（空）",
                                 ",".join(map(str, lines)))}
        return {"verdict": "questionable", "anchors": lines,
                "detail": "声明 %s；脚本 fix move 速度 %s（L%s），应变率需由几何推导"
                          % (declared_micro[:90], " ".join(used[:4]) or "（空）",
                             ",".join(map(str, lines)))}
    if velocities:
        lines = [v["line"] for v in velocities]
        return {"verdict": "questionable" if declared_rate is not None else "ok", "anchors": lines,
                "detail": "声明 %s；脚本 velocity set %s（L%s），应变率需由几何推导"
                          % (declared_micro[:90],
                             "；".join(" ".join(v["vector"]) for v in velocities[:3]),
                             ",".join(map(str, lines)))}
    if deform:
        return {"verdict": "questionable", "anchors": [d["line"] for d in deform],
                "detail": "声明 %s；脚本 fix deform 的 erate 无法解析" % declared_micro[:90]}
    return {"verdict": "unproven", "anchors": [],
            "detail": "声明加载 %s；脚本未找到 deform/move/velocity" % declared_micro[:90]}


def _observation_check(intent: dict, text: str):
    targets = (intent.get("objective") or {}).get("target_property") or []
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        return None
    lowered = text.lower()
    computable = []
    if re.search(r"stress|pxx|pyy|pzz|pressure", lowered):
        computable.append("stress")
    if re.search(r"strain|\blx\b|\bly\b|\blz\b", lowered):
        computable.append("strain")
    if re.search(r"msd|diffus|rdf|g\(r\)|coordination", lowered):
        computable.append("structure/dynamics")
    detail = "intent 目标量 %s；脚本输出线索 %s" % (
        "、".join(map(str, targets)), "、".join(computable) or "（未找到 stress/strain/structure 输出）")
    needs = []
    for target in targets:
        t = str(target).lower()
        if "stress_strain" in t and not {"stress", "strain"} <= set(computable):
            needs.append("stress+strain")
        elif ("elastic" in t or "modulus" in t) and not ({"stress", "strain"} & set(computable)):
            needs.append("stress/strain")
        elif "diffusion" in t and "structure/dynamics" not in computable:
            needs.append("diffusion")
    if needs:
        return {"verdict": "questionable", "anchors": [], "detail": detail + "；缺少 " + "、".join(needs)}
    return {"verdict": "ok", "anchors": [], "detail": detail}


def _ensembles(text: str):
    out = []
    for ln, tok in _commands(text):
        if tok and tok[0] == "fix" and len(tok) >= 4 and tok[3] in ("nvt", "npt", "nve", "nph"):
            out.append((ln, tok[3], tok))
    return out


def _declared(intent: dict):
    seq = intent.get("ensemble_sequence") or []
    return {str(s.get("ensemble", "")).lower() for s in seq if s.get("ensemble")}


def build_consistency(intent: dict, infile_text: str) -> list[ConsistencyItem]:
    rules = _rules()
    kp = intent.get("key_params") or {}
    goal = str((intent.get("objective") or {}).get("simulation_goal") or "").lower()
    exp_type = str((intent.get("experiment_design") or {}).get("experiment_type") or "").strip()
    declared = _declared(intent)
    script = _ensembles(infile_text)
    script_set = {e for _, e, _ in script}
    items: list[ConsistencyItem] = []
    n = 0

    def add(element, claim, detail, anchors=None, verdict="ok"):
        nonlocal n
        n += 1
        items.append(ConsistencyItem(
            id="c-%02d" % n, element=element, claim=claim, detail=detail,
            anchors=anchors or [], verdict=verdict))

    # 1) plan-internal: goal vs sequence
    goal_wants_npt = "npt" in goal
    if goal_wants_npt and "npt" not in declared:
        npt_lines = [ln for ln, e, _ in script if e == "npt"]
        add("系综", "目标要求 NPT（测量平衡晶格/密度）",
            "ensemble_sequence 仅声明 %s；in 文件含 NPT（L%s）→ 方案字段过期，需修订；实现与目标一致，不是仿真不成立"
            % ("、".join(sorted(declared)) or "（空）", ",".join(map(str, npt_lines)) or "无"), npt_lines, "plan_conflict")

    # 2) in-file vs goal
    if goal_wants_npt:
        if "npt" in script_set:
            anchors = [ln for ln, e, _ in script if e == "npt"]
            add("系综", "目标要求 NPT", "in 文件包含 NPT（L%s），实现与目标一致" %
                ",".join(map(str, anchors)), anchors, "ok")
        else:
            add("系综", "目标要求 NPT", "in 文件未找到 NPT（实际：%s）" %
                ("、".join(sorted(script_set)) or "无积分 fix"), [], "invalid")

    # 3) script vs declared sequence (merged into the plan-conflict item above when NPT is the target)
    extra = script_set - declared
    if extra and not (goal_wants_npt and "npt" in extra):
        anchors = [ln for ln, e, _ in script if e in extra]
        add("系综", "脚本系综需在 ensemble_sequence 中声明",
            "脚本含未声明系综 %s（L%s）——判定为方案字段待更新（plan_conflict），不是仿真不成立"
            % ("、".join(sorted(extra)), ",".join(map(str, anchors))), anchors, "plan_conflict")

    # 3.5) general ensemble consistency (declared vs script)
    if script_set:
        missing = declared - script_set
        if script_set <= declared:
            anchors = [ln for ln, e, _ in script]
            add("系综", "ensemble_sequence 声明 %s" % ("、".join(sorted(declared)) or "（空）"),
                "脚本系综 %s 与声明一致" % "、".join(sorted(script_set)), anchors, "ok")
        if missing:
            add("系综", "ensemble_sequence 声明 %s" % "、".join(sorted(missing)),
                "声明中的 %s 未在脚本中出现（可能仅用于其它阶段）" % "、".join(sorted(missing)),
                [], "questionable")
    elif declared:
        add("系综", "ensemble_sequence 声明 %s" % "、".join(sorted(declared)),
            "脚本中未找到 nvt/npt/nve 积分 fix，无法核对", [], "unproven")

    # 4) timestep / steps / units / pair_style
    variables = {}
    for ln, tok in _commands(infile_text):
        if tok[0] == "variable" and len(tok) >= 4 and tok[2] in ("equal", "index"):
            variables[tok[1]] = " ".join(tok[3:])

    def resolve(expr):
        for _ in range(10):
            nxt = re.sub(r"\$\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), expr)
            if nxt == expr:
                break
            expr = nxt
        if re.fullmatch(r"[0-9eE\s\.\+\-\*/\(\)]+", expr or ""):
            try:
                return float(eval(expr, {"__builtins__": {}}, {}))
            except Exception:
                return None
        return None

    script_dt = None
    script_steps = 0
    for ln, tok in _commands(infile_text):
        if tok[0] == "timestep" and len(tok) >= 2:
            v = resolve(tok[1])
            if v is not None:
                script_dt = v * 1000.0
        if tok[0] == "run" and len(tok) >= 2:
            v = resolve(tok[1])
            if v is not None:
                script_steps += int(round(v))
    if kp.get("timestep_fs") is not None:
        ok = script_dt is not None and abs(script_dt - float(kp["timestep_fs"])) < 1e-6
        add("时间步", "intent.timestep_fs = %s fs" % kp["timestep_fs"],
            "脚本 timestep = %s fs" % (script_dt if script_dt is not None else "未设置"),
            [ln for ln, tok in _commands(infile_text) if tok[0] == "timestep"], "ok" if ok else "invalid")
    if kp.get("total_steps"):
        ok = script_steps == int(kp["total_steps"])
        add("步数", "intent.total_steps = %s" % kp["total_steps"],
            "脚本 run 合计 = %s 步" % script_steps,
            [ln for ln, tok in _commands(infile_text) if tok[0] == "run"], "ok" if ok else "invalid")

    for key, cmd in (("units", "units"), ("pair_style", "pair_style")):
        expected = kp.get(key)
        if not expected:
            continue
        vals = [(ln, tok) for ln, tok in _commands(infile_text) if tok[0] == cmd]
        ok = any(len(t) >= 2 and t[1] == str(expected).split()[0] for _, t in vals)
        add("基本设置", "intent.%s = %s" % (key, expected),
            "脚本 %s = %s" % (cmd, ", ".join(" ".join(t[1:]) for _, t in vals) or "未设置"),
            [ln for ln, _ in vals], "ok" if ok else "invalid")

    # 4.1) declared potential file/style vs pair_coeff
    ff = _forcefield_check(intent, infile_text)
    if ff:
        add("势函数", "intent.key_params.forcefield", ff["detail"], ff["anchors"], ff["verdict"])

    # 4.2) declared temperatures vs integration fixes
    temp = _temperature_check(intent, infile_text)
    if temp:
        add("温度", "intent.ensemble_sequence[].T_K", temp["detail"], temp["anchors"], temp["verdict"])

    # 4.3) declared loading vs deform/move/velocity
    load = _loading_check(intent, infile_text)
    if load:
        add("加载速率", "intent.cross_scale.loading", load["detail"], load["anchors"], load["verdict"])

    # 4.4) declared observable vs output capability
    obs = _observation_check(intent, infile_text)
    if obs:
        add("观测量", "intent.objective.target_property", obs["detail"], obs["anchors"], obs["verdict"])

    # 5) command purpose whitelist
    disallow = rules.get("experiment_type_disallow", {})
    purpose = rules.get("command_purpose", {})
    for ln, tok in _commands(infile_text):
        if not (tok and tok[0] == "fix" and len(tok) >= 4):
            continue
        style = tok[3]
        spec = purpose.get("fix." + style)
        if not spec:
            continue
        allowed = spec.get("experiment_types", [])
        if exp_type and allowed and exp_type not in allowed:
            blocked = disallow.get(exp_type, [])
            if ("fix." + style) in blocked or "*" in blocked:
                add("命令用途", "实验类型为 %s" % exp_type,
                    "`fix %s` 仅适用于 %s 类实验；当前用于 %s → 命令用途不符（硬错误）"
                    % (style, "、".join(allowed), exp_type), [ln], "invalid")
            else:
                add("命令用途", "实验类型为 %s" % exp_type,
                    "`fix %s` 不属于该类型推荐用法，需说明" % style, [ln], "questionable")
        elif not exp_type and style == "indent":
            add("命令用途", "实验类型未声明",
                "`fix indent` 仅适用于压痕类；intent 未声明类型，无法核对 → 未论证", [ln], "unproven")

    if exp_type in ("drawing", "tension", "compression"):
        for ln, tok in _commands(infile_text):
            if tok and tok[0] == "fix" and len(tok) >= 4 and tok[3] == "move" and tok[2] == "all":
                add("命令用途", "加载应作用于夹持组",
                    "`fix %s all move` 作用于全体原子，会抑制变形（应为出口夹持组）" % tok[1],
                    [ln], "invalid")

    return items
