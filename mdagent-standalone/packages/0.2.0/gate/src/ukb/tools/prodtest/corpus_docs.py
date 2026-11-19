"""Document-tier items (cases and references) for the production test.

Cases use real files from automd-microbench-mini runs (copied into the intake folder at run time).
References are short PDFs generated here: they are paraphrased study notes, not the papers themselves
(no copyrighted text), each marked "注入测试资料".
"""
import os

from .corpus_entries import BATCH, D, LMP, ORIGIN_BENCH, T

RUNS = "automd-microbench-mini/runs"


def _case(title, summary, goal, setup, outcome, findings, rel, reuse, tags, files, sources, conditions):
    return {"type_id": "case", "title": title,
            "fields": {"summary": summary, "goal": goal, "setup": setup, "outcome": outcome, "findings": findings,
                       "result_reliability": rel, "reuse": reuse},
            "tags": [D, LMP] + tags, "files": files, "sources": sources, "conditions": conditions,
            "origin": ORIGIN_BENCH}


def cases(bench_root):
    """bench_root: absolute path of automd-microbench-mini. Returns (payloads, files_to_copy {name: src})."""
    r = lambda p: os.path.join(bench_root, "runs", *p.split("/"))
    s = lambda p: os.path.join(bench_root, "suite", *p.split("/"))
    copies = {
        "npt/in.npt": r("md01-cu-npt/20260929-190339/ws/run/in.npt"),
        "npt/log.npt": r("md01-cu-npt/20260929-190339/ws/run/log.npt"),
        "npt/report.md": r("md01-cu-npt/20260929-190339/ws/submission/scientific_report.md"),
        "npt/Cu_u3.eam": s("md01-cu-npt/assets/Cu_u3.eam"),
        "tension/in.tension": r("md03-cu-tension/20260929-202456/ws/in.tension"),
        "tension/log.lammps": r("md03-cu-tension/20260929-202456/ws/log.lammps"),
        "tension/report.md": r("md03-cu-tension/20260929-202456/ws/submission/scientific_report.md"),
        "tension/attempt1_failed_log.lammps":
            r("md03-cu-tension/20260929-202456/ws/submission/artifacts/attempt1_failed_log.lammps"),
        "tension/sigma_eps.txt": r("md03-cu-tension/20260929-202456/ws/submission/artifacts/sigma_eps.txt"),
        "elastic/elastic.in": r("md02-cu-elastic/20260929-222439/ws/scripts/elastic.in"),
        "elastic/a0_relax.in": r("md02-cu-elastic/20260929-222439/ws/scripts/a0_relax.in"),
        "elastic/analysis.py": r("md02-cu-elastic/20260929-222439/ws/submission/artifacts/analysis.py"),
        "elastic/report.md": r("md02-cu-elastic/20260929-222439/ws/submission/scientific_report.md"),
        "restart/in_fixed.in": r("md16r-restart-recover/20260929-215801/ws/run1/in_fixed.in"),
        "restart/in_restart.in": r("md16r-restart-recover/20260929-215801/ws/run2/in_restart.in"),
        "restart/log_fixed.txt": r("md16r-restart-recover/20260929-215801/ws/run1/log_fixed.txt"),
        "restart/report.md": r("md16r-restart-recover/20260929-215801/ws/submission/scientific_report.md"),
        "restart/in_broken.in": s("md16r-restart-recover/assets/in_broken.in"),
    }

    def f(path, role, desc, reusable=None):
        d = {"path": path.split("/", 1)[1], "role": role, "description": desc, "src": path}
        if reusable is not None:
            d["reusable"] = reusable
        return d

    out = [
        _case("fcc Cu NPT 300 K 平衡密度与热晶格常数（864 原子，已验证）",
              "Cu_u3.eam，6x6x6 超胞 NPT 300 K/0 bar 连续 12000 步，后半段统计得到密度与晶格常数，与评测参考吻合。",
              "300 K、0 bar 下的平衡密度与热晶格常数。",
              "a0 取势的 0 K 值 [[in.npt:L14]]，超胞 [[in.npt:L16]]，Nose-Hoover NPT Tdamp 0.1/Pdamp 1.0 ps "
              "[[in.npt:L38]]，步长 [[in.npt:L31]]，总步数 [[in.npt:L40]]。",
              "密度 8.805 g/cm³、晶格常数 3.6327 Å，详见报告 [[report.md]]。",
              ["前后半窗平衡判据通过，温度均值在 300±10 K。"],
              "verified",
              {"entry_files": ["in.npt", "Cu_u3.eam"],
               "parameters_to_adapt": [{"name": "温度", "location": "in.npt:L38", "meaning": "NPT 目标温度", "original": "300"},
                                       {"name": "超胞", "location": "in.npt:L16", "meaning": "体系尺寸", "original": "6x6x6"},
                                       {"name": "势文件路径", "location": "in.npt:L23", "meaning": "相对路径需改为本地",
                                        "original": "../assets/Cu_u3.eam"}],
               "prerequisites": ["LAMMPS（MANYBODY 包）"], "verified_env": "LAMMPS 2 Aug 2023, Windows MSMPI",
               "known_limits": ["864 原子有限尺寸", "单种子"]},
              [T("topic", "equilibration"), T("property", "lattice-density"), T("object", "fcc-metal")],
              [f("npt/in.npt", "input", "NPT 主输入"), f("npt/Cu_u3.eam", "parameter_file", "Foiles 1986 Cu EAM（funcfl）"),
               f("npt/log.npt", "log", "完整运行日志"), f("npt/report.md", "report", "科学报告")],
              [{"source_type": "run_artifact", "title": "md01 运行目录", "locator": RUNS + "/md01-cu-npt/20260929-190339"}],
              [{"key": "temperature_k", "value": 300}, {"key": "pressure_bar", "value": 0},
               {"key": "units_style", "value": "metal"}, {"key": "natoms", "value": 864}]),
        _case("Cu [100] 单晶 300 K 单轴应变拉伸（含一次失败尝试）",
              "1200 原子 Cu 单晶 NPT 预平衡后 NVT+deform 拉伸到 ε=0.11，得到弹性段斜率与峰值应力；首次尝试因 pair_coeff 写法报错。",
              "得到 [100] 单轴应变拉伸的应力应变曲线、弹性段斜率与峰值。",
              "超胞 5x5x12 [[in.tension:L20]]，NPT 预平衡 [[in.tension:L37]]，NVT [[in.tension:L52]] + deform "
              "erate 0.005/ps [[in.tension:L53]]，应力换号 [[in.tension:L57]]。",
              "斜率 148.8 GPa（参考 154），峰值 12.68 GPa @ ε=0.11（右删失），见 [[report.md]]；首次失败见 "
              "[[attempt1_failed_log.lammps]]。",
              ["应变率比实验高 11~13 个数量级，峰值是上限", "横向固定得到的是单轴应变模量"],
              "single_run",
              {"entry_files": ["in.tension"],
               "parameters_to_adapt": [{"name": "应变率", "location": "in.tension:L53", "meaning": "加载速率", "original": "0.005"},
                                       {"name": "超胞", "location": "in.tension:L20", "meaning": "尺寸", "original": "5x5x12"},
                                       {"name": "种子", "location": "in.tension:L17", "meaning": "速度种子", "original": "972154"}],
               "prerequisites": ["势文件 assets/Cu_u3.eam"], "verified_env": "LAMMPS 2 Aug 2023",
               "known_limits": ["单种子", "峰值右删失", "非单轴应力"]},
              [T("topic", "mechanical-loading"), T("topic", "strain-rate"), T("property", "strength"),
               T("property", "elastic"), T("object", "fcc-metal")],
              [f("tension/in.tension", "input", "拉伸输入（两阶段）"),
               f("tension/log.lammps", "log", "运行日志"),
               f("tension/sigma_eps.txt", "data", "应力应变曲线数据（每 20 步）"),
               f("tension/attempt1_failed_log.lammps", "log", "第一次失败的日志（pair_coeff 报错）"),
               f("tension/report.md", "report", "科学报告")],
              [{"source_type": "run_artifact", "title": "md03 运行目录", "locator": RUNS + "/md03-cu-tension/20260929-202456"}],
              [{"key": "temperature_k", "value": 300}, {"key": "strain_rate_per_ps", "value": 0.005},
               {"key": "natoms", "value": 1200}, {"key": "timestep_ps", "value": 0.001}]),
        _case("Cu 0 K 弹性常数：能量-应变三模式拟合",
              "Cu_u3.eam 4x4x4 超胞，静水、四方、三斜剪切三种应变各 11 点，二次拟合得到 C11/C12/C44 并判定 Born 稳定。",
              "得到 0 K 弹性常数并检查机械稳定性。",
              "应变点 [[elastic.in:L10]]，三种模式 [[elastic.in:L31-33]]，循环内重设 units [[elastic.in:L12]]。",
              "C11=167.3、C12=124.2、C44=76.4 GPa，均在参考 ±6% 内，见 [[report.md]]；拟合脚本 [[analysis.py]]。",
              ["clear 会重置 units，必须在循环内重设", "倾斜量默认是 lattice 单位"],
              "verified",
              {"entry_files": ["elastic.in", "analysis.py", "a0_relax.in"],
               "parameters_to_adapt": [{"name": "a0", "location": "elastic.in:L2", "meaning": "命令行传入的平衡晶格常数",
                                        "original": "-var a0 3.61516"},
                                       {"name": "应变幅度", "location": "elastic.in:L10", "meaning": "应变点", "original": "±0.010"}],
               "prerequisites": ["Python 3 + numpy（analysis.py）"], "verified_env": "LAMMPS 2 Aug 2023",
               "known_limits": ["只适用于立方晶体", "0 K，无热软化"]},
              [T("method", "energy-strain"), T("property", "elastic"), T("object", "fcc-metal"),
               T("topic", "potential-validation")],
              [f("elastic/elastic.in", "input", "能量-应变主脚本"), f("elastic/a0_relax.in", "input", "a0 弛豫"),
               f("elastic/analysis.py", "script", "拟合与 Born 判据"), f("elastic/report.md", "report", "科学报告")],
              [{"source_type": "run_artifact", "title": "md02 运行目录", "locator": RUNS + "/md02-cu-elastic/20260929-222439"}],
              [{"key": "temperature_k", "value": 0}, {"key": "potential_family", "value": "eam"}]),
        _case("LJ 体系崩溃修复与断点续算连续性验证",
              "原始输入 dt*=0.025 + T*=5.0 导致 Lost atoms；修复为 dt*=0.005、T*=1.0 并每 2000 步写 restart，从 4000 步续算到 6000 步，接缝差 0。",
              "修复崩溃并验证 restart 连续性。",
              "原始错误输入 [[in_broken.in]]，修复后速度与步长 [[in_fixed.in:L17]] [[in_fixed.in:L19]]，写 restart "
              "[[in_fixed.in:L22]]，续算读入 [[in_restart.in:L3]]。",
              "续算段与连续段接缝能量一致，NVE 漂移约 3e-3，见 [[report.md]]。",
              ["续算时不要 reset_timestep", "fix 需在续算输入中重新定义"],
              "verified",
              {"entry_files": ["in_fixed.in", "in_restart.in"],
               "parameters_to_adapt": [{"name": "restart 间隔", "location": "in_fixed.in:L22", "meaning": "写出频率",
                                        "original": "2000"},
                                       {"name": "续算文件", "location": "in_restart.in:L3", "meaning": "读入哪个 checkpoint",
                                        "original": "restart.4000"}],
               "verified_env": "LAMMPS 2 Aug 2023", "known_limits": ["restart 文件与版本绑定"]},
              [T("topic", "restart"), T("topic", "integration"), T("topic", "run-failure"), T("object", "lj-model")],
              [f("restart/in_broken.in", "input", "原始错误输入", reusable=False),
               f("restart/in_fixed.in", "input", "修复后输入"), f("restart/in_restart.in", "input", "续算输入"),
               f("restart/log_fixed.txt", "log", "修复后运行日志"), f("restart/report.md", "report", "诊断与连续性报告")],
              [{"source_type": "run_artifact", "title": "md16r 运行目录",
                "locator": RUNS + "/md16r-restart-recover/20260929-215801"}],
              [{"key": "units_style", "value": "lj"}, {"key": "natoms", "value": 256}]),
    ]
    return out, copies


REFERENCES = [
    {"file": "foiles1986_notes.pdf", "title": "Foiles, Baskes, Daw 1986：fcc 金属 EAM 势（学习笔记）",
     "citation": "S. M. Foiles, M. I. Baskes, M. S. Daw, Phys. Rev. B 33, 7983 (1986)",
     "identifier": "doi:10.1103/PhysRevB.33.7983",
     "pages": ["注入测试资料：本 PDF 是学习笔记，不是原文。Foiles-Baskes-Daw 1986 为 Cu、Ag、Au、Ni、Pd、Pt 及其合金拟合 EAM 势。",
               "拟合目标：晶格常数、升华能、体弹模量、弹性常数、空位形成能。Cu 的势常以 funcfl 文件 Cu_u3.eam 分发。",
               "已知局限：表面能、层错能与部分合金混合热的准确性有限；适用于近平衡固态。"],
     "summary": "为六种 fcc 金属及其合金构造 EAM 势，按晶格常数、升华能、弹性常数和空位形成能拟合。",
     "key_points": ["拟合到晶格常数、升华能、体弹模量、弹性常数与空位形成能 [[foiles1986_notes.pdf#p2]]",
                    "Cu 势以 funcfl 文件 Cu_u3.eam 分发 [[foiles1986_notes.pdf#p2]]",
                    "表面能与层错能准确性有限 [[foiles1986_notes.pdf#p3]]"],
     "limitations": "近平衡固态；表面、层错、高温液态需另行验证。",
     "tags": [T("topic", "potential"), T("topic", "potential-format"), T("object", "fcc-metal")]},
    {"file": "mishin2001_notes.pdf", "title": "Mishin 2001：Cu 的 EAM 势与层错能（学习笔记）",
     "citation": "Y. Mishin et al., Phys. Rev. B 63, 224106 (2001)", "identifier": "doi:10.1103/PhysRevB.63.224106",
     "pages": ["注入测试资料：本 PDF 是学习笔记，不是原文。该工作为 Cu 拟合 EAM 势，同时纳入第一性原理数据。",
               "拟合集包含层错能与多种结构能量差，层错能约 44 mJ/m2，接近实验。",
               "常用于位错、层错、孪晶相关模拟；setfl 格式。"],
     "summary": "用实验与第一性原理数据拟合 Cu EAM 势，层错能与结构能量差描述较好。",
     "key_points": ["拟合集含第一性原理结构能量差 [[mishin2001_notes.pdf#p1]]",
                    "层错能约 44 mJ/m² [[mishin2001_notes.pdf#p2]]", "以 setfl 格式分发 [[mishin2001_notes.pdf#p3]]"],
     "data_values": [{"quantity": "固有层错能", "value": "44", "unit": "mJ/m²", "locator": "mishin2001_notes.pdf#p2"}],
     "limitations": "单元素 Cu；合金需配套交叉势。",
     "tags": [T("topic", "potential"), T("topic", "defects"), T("property", "defect-energy"), T("object", "fcc-metal")]},
    {"file": "stukowski2010_ovito_notes.pdf", "title": "Stukowski 2010：OVITO 与结构识别（学习笔记）",
     "citation": "A. Stukowski, Modelling Simul. Mater. Sci. Eng. 18, 015012 (2010)",
     "identifier": "doi:10.1088/0965-0393/18/1/015012",
     "pages": ["注入测试资料：本 PDF 是学习笔记，不是原文。OVITO 是原子模拟可视化与分析平台。",
               "常用修改器：CNA（含自适应 a-CNA）、PTM、DXA 位错提取、Wigner-Seitz 缺陷分析。"],
     "summary": "介绍 OVITO 平台及其结构识别、位错提取等分析功能。",
     "key_points": ["提供 a-CNA、PTM 等结构识别 [[stukowski2010_ovito_notes.pdf#p2]]",
                    "DXA 提取位错线 [[stukowski2010_ovito_notes.pdf#p2]]"],
     "tags": [T("topic", "structure-analysis"), T("tool", "ovito")]},
    {"file": "strain_rate_review_notes.pdf", "title": "MD 应变率效应综述要点（学习笔记）",
     "citation": "综合整理（多篇文献），注入测试资料",
     "pages": ["注入测试资料：本 PDF 是整理笔记。MD 拉伸应变率通常 1e7~1e10 /s，实验 1e-4~1e0 /s。",
               "高应变率下位错形核推迟，峰值应力随应变率对数增加；外推需多个应变率。"],
     "summary": "整理 MD 应变率与实验的差距及其对强度的影响。",
     "key_points": ["MD 应变率 1e7~1e10 /s，实验 1e-4~1e0 /s [[strain_rate_review_notes.pdf#p1]]",
                    "峰值应力随应变率对数增加 [[strain_rate_review_notes.pdf#p2]]"],
     "limitations": "定性整理，未给出具体材料的标定数据。",
     "tags": [T("topic", "strain-rate"), T("property", "strength")]},
]


def make_pdf(path, pages):
    import fitz
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(56, 56, 540, 780), text, fontsize=11, fontname="china-s")
    doc.save(path)
    doc.close()


def references(inbox):
    out = []
    for r in REFERENCES:
        p = os.path.join(inbox, r["file"])
        make_pdf(p, r["pages"])
        fields = {"citation": r["citation"], "summary": r["summary"], "key_points": r["key_points"]}
        for k in ("identifier", "data_values", "limitations"):
            if k in r:
                fields[k] = r[k]
        out.append({"type_id": "reference", "title": r["title"], "fields": fields, "tags": [D] + r["tags"],
                    "files": [{"path": r["file"], "role": "fulltext", "description": "学习笔记 PDF（非原文）",
                               "source_path": p, "license_note": "自制笔记，可内部使用"}],
                    "origin": {"kind": "document", "ref": BATCH, "summary_of_evidence": "注入测试资料：自制学习笔记"}})
    return out
