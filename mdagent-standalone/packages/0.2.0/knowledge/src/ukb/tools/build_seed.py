"""Regenerates data/seed/*.json. The seed pack carries only domain-neutral defaults.

Domain packs (tags for concrete fields, risk rules, extra formats) go in data/domain/<name>/ and are loaded
with `ukb load-pack`.
"""
import json
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seed")


def w(name, obj):
    path = os.path.join(OUT, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def txt(req=False, ml=None, d=None):
    out = {"kind": "text", "required": req}
    if ml:
        out["max_len"] = ml
    if d:
        out["description"] = d
    return out


def lst(d=None, req=False, item="text"):
    out = {"kind": "list", "item": item, "required": req}
    if d:
        out["description"] = d
    return out


CHECK = ["一条只讲一件事", "标题准确概括内容", "适用条件写清，或明确说明普适", "来源可回读",
         "已读过同范围条目，无未处理的重复或矛盾", "标签按标签说明归类"]


def T(tid, label, desc, fields, sources=True, tier="entry", doc=None, extra_check=()):
    spec = {"tier": tier, "fields": fields, "required_facets": ["topic"], "render": list(fields),
            "review_checklist": CHECK + list(extra_check), "require_sources": sources}
    if doc:
        spec["document"] = doc
    w("types/%s.json" % tid, {"type_id": tid, "label": label, "description": desc, "spec": spec})


def main():
    w("setting.json", [])
    def tf(fid, label, pats, mb=None):
        out = {"format_id": fid, "label": label, "patterns": pats, "kind": "text"}
        if mb:
            out["max_bytes"] = mb
        return out

    mb = 1024 * 1024
    w("file_format.json", [
        tf("markdown", "Markdown 文档", ["*.md", "*.markdown"]),
        tf("plain_text", "纯文本", ["*.txt", "*.rst"]),
        tf("tabular_text", "表格文本", ["*.csv", "*.tsv"], 5 * mb),
        tf("structured_text", "结构化文本", ["*.json", "*.yaml", "*.yml", "*.toml", "*.xml", "*.ini", "*.cfg", "*.conf"]),
        tf("script", "脚本与源码", ["*.py", "*.sh", "*.bash", "*.ps1", "*.bat", "*.r", "*.m", "*.jl", "*.js", "*.ts",
                                "*.c", "*.h", "*.cpp", "*.hpp", "*.f", "*.f90", "*.tcl", "*.mk", "makefile",
                                "dockerfile", "*.slurm", "*.pbs", "*.job"]),
        tf("input_text", "计算输入与模型文件（文本）",
           ["in.*", "*.in", "*.inp", "*.input", "*.ctrl", "*.mdp", "*.top", "*.itp", "*.param", "*.par", "*.def",
            "*.data", "*.lmp", "*.pdb", "*.gro", "*.xyz", "*.cif", "*.poscar", "poscar", "incar", "kpoints",
            "*.vasp", "*.mol2", "*.sdf"], 5 * mb),
        tf("log_text", "运行日志（文本，建议只截关键部分）", ["*.log", "log.*", "*.out", "*.err", "*.lammps"], 2 * mb),
        tf("parameter_text", "参数集/势函数（文本）",
           ["*.eam", "*.alloy", "*.fs", "*.meam", "*.tersoff", "*.sw", "*.reax", "*.ff", "*.potential"], 10 * mb),
        {"format_id": "pdf", "label": "PDF 文档", "patterns": ["*.pdf"], "kind": "pdf", "magic_hex": "25504446",
         "max_bytes": 30 * mb},
        {"format_id": "png", "label": "PNG 图片", "patterns": ["*.png"], "kind": "binary", "magic_hex": "89504e47",
         "max_bytes": 5 * mb},
        {"format_id": "jpeg", "label": "JPEG 图片", "patterns": ["*.jpg", "*.jpeg"], "kind": "binary",
         "magic_hex": "ffd8ff", "max_bytes": 5 * mb},
        tf("svg", "SVG 矢量图", ["*.svg"], 5 * mb),
    ])
    deny = [
        ("*.dump", "轨迹/转储文件不入库：体量大、可由输入复现，留在计算工作区"),
        ("dump.*", "轨迹/转储文件不入库"), ("*.lammpstrj", "轨迹文件不入库"), ("*.dcd", "轨迹文件不入库"),
        ("*.xtc", "轨迹文件不入库"), ("*.trr", "轨迹文件不入库"), ("*.trj", "轨迹文件不入库"),
        ("*.traj", "轨迹文件不入库"), ("xdatcar", "轨迹文件不入库"), ("*.nc", "轨迹/大数据文件不入库"),
        ("*.netcdf", "大数据文件不入库"), ("*.h5", "大数据文件不入库"), ("*.hdf5", "大数据文件不入库"),
        ("*.restart", "重启文件不入库：二进制且与软件版本绑定"), ("restart.*", "重启文件不入库"),
        ("*.rst7", "重启文件不入库"), ("*.cpt", "检查点文件不入库"), ("*.chk", "检查点文件不入库"),
        ("wavecar", "大体量中间文件不入库"), ("chgcar", "大体量中间文件不入库"),
        ("*.tpr", "二进制运行输入不入库，请入库其文本源"), ("*.edr", "二进制能量文件不入库"),
        ("*.bin", "二进制数据不入库"), ("*.npy", "数组数据不入库"), ("*.npz", "数组数据不入库"),
        ("*.pkl", "序列化对象不入库"), ("*.pickle", "序列化对象不入库"),
        ("*.zip", "压缩包不入库：请解包后按文件提交"), ("*.tar", "压缩包不入库"), ("*.gz", "压缩文件不入库"),
        ("*.tgz", "压缩包不入库"), ("*.bz2", "压缩文件不入库"), ("*.xz", "压缩文件不入库"),
        ("*.7z", "压缩包不入库"), ("*.rar", "压缩包不入库"),
        ("*.exe", "可执行文件不入库"), ("*.dll", "可执行文件不入库"), ("*.so", "可执行文件不入库"),
        ("*.dylib", "可执行文件不入库"), ("a.out", "可执行文件不入库"),
        ("*.mp4", "视频不入库"), ("*.avi", "视频不入库"), ("*.mov", "视频不入库"), ("*.gif", "动图不入库"),
        (".env", "凭证文件不入库"), ("*.pem", "密钥文件不入库"), ("*.key", "密钥文件不入库"),
        ("id_rsa*", "密钥文件不入库"), ("*.p12", "密钥文件不入库"),
        ("*.sqlite", "数据库文件不入库"), ("*.db", "数据库文件不入库"),
    ]
    w("file_deny.json", [{"pattern": p, "reason": r} for p, r in deny])
    w("artifact_role.json", [
        {"role_id": "input", "label": "输入/控制文件", "description": "可直接复用的输入、控制、配置文件",
         "default_reusable": True},
        {"role_id": "script", "label": "脚本", "description": "运行、前处理、后处理脚本", "default_reusable": True},
        {"role_id": "config", "label": "配置", "description": "环境或作业配置", "default_reusable": True},
        {"role_id": "structure", "label": "结构/模型文件", "description": "初始结构、模型定义（文本格式，体量受限）",
         "default_reusable": True},
        {"role_id": "parameter_file", "label": "参数文件", "description": "参数集、势函数等（注意许可）",
         "default_reusable": True},
        {"role_id": "data", "label": "结果数据（小）", "description": "汇总后的小体量结果表，不含轨迹",
         "default_reusable": False},
        {"role_id": "log", "label": "日志摘录", "description": "运行日志或其关键部分", "default_reusable": False},
        {"role_id": "figure", "label": "图", "description": "结果图、示意图", "default_reusable": False},
        {"role_id": "report", "label": "报告/说明", "description": "案例报告、说明文档", "default_reusable": False},
        {"role_id": "fulltext", "label": "全文", "description": "文献、手册、标准的全文（PDF 或文本）",
         "default_reusable": False},
        {"role_id": "supplement", "label": "补充材料", "description": "文献补充材料", "default_reusable": False},
        {"role_id": "excerpt", "label": "摘录", "description": "原文的节选或转写文本", "default_reusable": False},
    ])
    w("facet.json", [
        {"facet_id": "domain", "label": "领域", "description": "条目所属学科或业务领域。粗分即可；具体领域标签由域数据包提供。",
         "multi": True, "governing": True, "scope_budget_chars": 60000, "ord": 1},
        {"facet_id": "topic", "label": "主题", "description": "条目讨论的主题或问题类别。每条至少一个。",
         "multi": True, "governing": False, "scope_budget_chars": 40000, "ord": 2},
        {"facet_id": "stage", "label": "工作阶段", "description": "条目适用于工作流的哪个阶段。",
         "multi": True, "governing": True, "scope_budget_chars": 60000, "ord": 3},
        {"facet_id": "method", "label": "方法", "description": "所用的方法或技术。", "multi": True, "governing": False,
         "scope_budget_chars": 40000, "ord": 4},
        {"facet_id": "object", "label": "对象", "description": "研究或处理的对象、系统、材料。", "multi": True,
         "governing": False, "scope_budget_chars": 40000, "ord": 5},
        {"facet_id": "tool", "label": "工具", "description": "涉及的软件、仪器或命令。", "multi": True,
         "governing": False, "scope_budget_chars": 40000, "ord": 6},
        {"facet_id": "consumer", "label": "使用方", "description": "条目主要面向哪个使用方（Agent 角色、审阅环节）。",
         "multi": True, "governing": True, "ord": 7},
    ])
    w("tag.json", [
        {"facet": "stage", "slug": "planning", "label": "方案设计", "description": "确定目标、方法与方案；不含具体执行。",
         "aliases": ["方案", "设计", "plan", "design"]},
        {"facet": "stage", "slug": "setup", "label": "准备与建模", "description": "准备输入、模型、环境。",
         "aliases": ["建模", "准备", "setup", "modeling"]},
        {"facet": "stage", "slug": "execution", "label": "执行", "description": "运行、监控、处理运行中的故障。",
         "aliases": ["运行", "执行", "run", "execution"]},
        {"facet": "stage", "slug": "analysis", "label": "分析", "description": "结果处理、分析与解释。",
         "aliases": ["分析", "后处理", "analysis", "postprocessing"]},
        {"facet": "stage", "slug": "validation", "label": "验证与验收", "description": "对照基准、核对、验收。",
         "aliases": ["验证", "验收", "validation", "verification"]},
        {"facet": "stage", "slug": "reporting", "label": "报告", "description": "撰写报告、交付。",
         "aliases": ["报告", "交付", "report"]},
        {"facet": "topic", "slug": "general", "label": "通用",
         "description": "暂时没有更合适主题时使用；审核时优先改挂到具体主题。", "aliases": ["通用", "general"]},
    ])
    w("condition_key.json", [
        {"key_id": "software_version", "label": "软件版本", "value_type": "text"},
        {"key_id": "scale_note", "label": "规模说明", "value_type": "text"},
        {"key_id": "validity_note", "label": "适用前提说明", "value_type": "text"},
    ])
    w("relation.json", [
        {"rel_id": "supersedes", "label": "取代", "inverse_label": "被取代", "acyclic": True},
        {"rel_id": "contradicts", "label": "矛盾", "inverse_label": "矛盾", "symmetric": True},
        {"rel_id": "refines", "label": "细化", "inverse_label": "被细化", "acyclic": True},
        {"rel_id": "depends_on", "label": "依赖", "inverse_label": "被依赖", "acyclic": True},
        {"rel_id": "example_of", "label": "是其示例", "inverse_label": "示例", "acyclic": True},
        {"rel_id": "adapted_from", "label": "改编自", "inverse_label": "被改编为", "acyclic": True},
        {"rel_id": "duplicate_of", "label": "重复于", "inverse_label": "有重复", "acyclic": True},
        {"rel_id": "cites", "label": "引用", "inverse_label": "被引用", "acyclic": False},
        {"rel_id": "related", "label": "相关", "inverse_label": "相关", "symmetric": True},
    ])
    w("source_type.json", [
        {"source_type_id": "user_confirmation", "label": "用户/所有者确认", "default_grade": 0},
        {"source_type_id": "literature", "label": "文献", "default_grade": 0},
        {"source_type_id": "measurement", "label": "实测数据", "default_grade": 0},
        {"source_type_id": "run_artifact", "label": "运行产物", "default_grade": 0},
        {"source_type_id": "case", "label": "过往案例", "default_grade": 0},
        {"source_type_id": "knowledge_entry", "label": "知识库条目", "default_grade": 0},
        {"source_type_id": "dialogue", "label": "对话/会话记录", "default_grade": 0},
        {"source_type_id": "estimate", "label": "估算/推导", "default_grade": 0},
        {"source_type_id": "external_doc", "label": "外部文档/手册", "default_grade": 0},
    ])
    w("profile.json", [
        {"profile_id": "agent", "spec": {"label": "默认 Agent 口径", "visible_statuses": ["active", "trial"],
                                         "include_suggested_tags": False, "read_budget_chars": 12000,
                                         "catalog_limit": 20, "read_max_entries": 3, "must_read": {}}},
        {"profile_id": "agent_wide", "spec": {"label": "宽口径（含已过时）",
                                              "visible_statuses": ["active", "trial", "deprecated"],
                                              "include_suggested_tags": True, "read_budget_chars": 12000,
                                              "catalog_limit": 30, "read_max_entries": 5, "must_read": {}}},
        {"profile_id": "reviewer", "spec": {"label": "审核口径（全部状态）",
                                            "visible_statuses": ["candidate", "trial", "active", "deprecated",
                                                                 "superseded", "revoked", "rejected", "quarantined"],
                                            "include_suggested_tags": True, "read_budget_chars": 60000,
                                            "must_read": {}}},
    ])
    w("risk_rule.json", [])

    T("observation", "观察", "一次观察到的事实，尚未上升为规律",
      {"statement": txt(True, 2000), "observed_at": txt(), "context": txt(False, 2000)})
    T("claim", "断言", "可被证实或证伪的判断",
      {"statement": txt(True, 2000), "scope_note": txt(False, 1000), "evidence_note": txt(False, 2000)})
    T("lesson", "经验教训", "情境 → 风险 → 对策",
      {"situation": txt(True, 1500), "risk": txt(True, 1500), "fix": txt(True, 2000),
       "check": txt(False, 1000, "如何确认问题已避免")})
    T("rule", "规则", "约束、禁则或必须项",
      {"statement": txt(True, 1500), "severity": {"kind": "enum", "values": ["hard", "soft", "info"], "required": True},
       "rationale": txt(True, 2000), "check": txt(False, 1000), "exceptions": txt(False, 1000)})
    T("procedure", "做法/流程", "步骤化做法",
      {"goal": txt(True, 800), "preconditions": lst(), "steps": lst(req=True, item={"kind": "text", "max_len": 1500}),
       "pitfalls": lst()})
    T("parameter", "参数/取值", "某个量的取值或范围",
      {"quantity": txt(True, 200), "unit": txt(), "value": {"kind": "number"}, "min": {"kind": "number"},
       "max": {"kind": "number"}, "method": txt(False, 1500, "如何得到该值"), "note": txt(False, 1500)})
    T("failure_pattern", "失败模式", "失败现象、原因与处理",
      {"signature": txt(True, 500, "可识别的失败表现"), "symptoms": lst(), "causes": lst(req=True),
       "fixes": lst(req=True)})
    T("checklist", "检查清单", "审查或验收清单",
      {"purpose": txt(True, 800), "items": lst(req=True, item={"kind": "object", "fields": {
          "question": {"kind": "text", "required": True}, "pass_criteria": {"kind": "text", "required": True}}})})
    T("definition", "术语定义", "术语与定义",
      {"term": txt(True, 200), "definition": txt(True, 2000), "aliases": lst()}, sources=False)
    T("faq", "问答", "常见问题与回答", {"question": txt(True, 800), "answer": txt(True, 3000)})
    T("correction", "纠正", "对某个条目或常见错误的纠正",
      {"wrong": txt(True, 1500), "right": txt(True, 1500), "target": {"kind": "ref"}, "why": txt(False, 1500)})
    T("template", "模板片段", "可复用的短片段",
      {"usage": txt(True, 1000), "content": txt(True, 6000), "placeholders": lst()}, sources=False)
    T("case", "案例", "一次完整任务或运行；摘要层供阅读，原文文件包供核对与复用",
      {"summary": txt(True, 800, "一段话：做了什么、结果如何"),
       "goal": txt(True, 600),
       "setup": txt(True, 1200, "对象、方法、关键参数；关键参数用 [[path:Lx-y]] 指向原文"),
       "outcome": txt(True, 1000, "结果与关键数值，带原文定位"),
       "findings": lst("结论；已提炼为独立条目的写其 entry_id"),
       "result_reliability": {"kind": "enum", "values": ["verified", "single_run", "known_bias", "failed", "unknown"],
                              "required": True},
       "reuse": {"kind": "object", "required": True, "fields": {
           "entry_files": lst("复用时从哪些文件入手", req=True),
           "parameters_to_adapt": lst(item={"kind": "object", "fields": {
               "name": {"kind": "text", "required": True}, "location": {"kind": "text", "required": True},
               "meaning": {"kind": "text", "required": True}, "original": {"kind": "text"},
               "note": {"kind": "text"}}}),
           "prerequisites": lst(), "verified_env": txt(), "known_limits": lst()}}},
      tier="document",
      doc={"summary_max_chars": 3000,
           "allowed_roles": ["input", "script", "config", "structure", "parameter_file", "data", "log", "figure",
                             "report"],
           "required_roles": ["input"], "summary_must_cite": True, "max_files": 200},
      extra_check=("摘要与原文一致，关键数值都有原文定位", "复用说明列全了必须修改的参数", "未包含轨迹等大体量原始输出"))
    T("reference", "参考文献", "已有的文献（通常是 PDF）：摘要层供阅读，原文供核对；知识库不负责获取论文",
      {"citation": txt(True, 800, "作者、题目、出处、年份"),
       "identifier": txt(False, 200, "DOI、arXiv 号等"),
       "summary": txt(True, 1200, "研究问题、方法、主要结论"),
       "key_points": lst("要点，每条带 [[path#pN]] 页码定位", req=True, item={"kind": "text", "max_len": 600}),
       "data_values": lst(item={"kind": "object", "fields": {
           "quantity": {"kind": "text", "required": True}, "value": {"kind": "text", "required": True},
           "unit": {"kind": "text"}, "conditions": {"kind": "text"}, "locator": {"kind": "text", "required": True}}}),
       "methods": txt(False, 1200), "limitations": txt(False, 800)},
      tier="document", sources=False,
      doc={"summary_max_chars": 4000, "allowed_roles": ["fulltext", "supplement", "excerpt", "figure"],
           "required_roles": ["fulltext"], "summary_must_cite": True,
           "allowed_formats": ["pdf", "markdown", "plain_text", "png", "jpeg"], "max_files": 20},
      extra_check=("引用信息完整", "要点与数值都能在原文对应页找到", "原文文件就是该文献本身"))
    T("document", "文档", "手册、标准、报告等长文档：摘要层 + 原文",
      {"summary": txt(True, 1500), "key_points": lst(req=True, item={"kind": "text", "max_len": 600}),
       "scope": txt(False, 800, "适用范围"), "version_note": txt(False, 300)},
      tier="document", sources=False,
      doc={"summary_max_chars": 3000, "allowed_roles": ["fulltext", "excerpt", "report", "figure", "supplement"],
           "required_roles": ["fulltext"], "summary_must_cite": True, "max_files": 30},
      extra_check=("摘要与原文一致", "要点都有原文定位"))


if __name__ == "__main__":
    main()
    print("seed written to", OUT)
