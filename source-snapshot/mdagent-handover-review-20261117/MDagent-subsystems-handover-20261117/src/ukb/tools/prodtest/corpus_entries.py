"""Seed corpus for the production test: metal MD knowledge, "roughly right", flagged for later cleanup.

Every item carries origin.ref = PRODTEST_BATCH so the whole batch can be found and revoked later.
Sources that point at the benchmark package are real paths; general-knowledge items cite textbooks or
documentation generically and are typed as such.
"""

BATCH = "prodtest-metal-md-20261031"

ORIGIN_TEXTBOOK = {"kind": "manual", "ref": BATCH,
                   "summary_of_evidence": "注入测试数据：依据领域常识与常见文献整理，大致正确，待人工清理"}
ORIGIN_BENCH = {"kind": "run", "ref": BATCH,
                "summary_of_evidence": "注入测试数据：来自 automd-microbench-mini 已验证运行与评测参考，待人工清理"}

SRC_DOC = {"source_type": "external_doc", "title": "LAMMPS 文档", "locator": "https://docs.lammps.org/"}
SRC_FOILES = {"source_type": "literature", "title": "Foiles, Baskes, Daw, PRB 33, 7983 (1986)",
              "locator": "doi:10.1103/PhysRevB.33.7983"}
SRC_MISHIN = {"source_type": "literature", "title": "Mishin et al., PRB 63, 224106 (2001) Cu EAM",
              "locator": "doi:10.1103/PhysRevB.63.224106"}
SRC_TEXT = {"source_type": "external_doc", "title": "Allen & Tildesley, Computer Simulation of Liquids",
            "locator": "isbn:9780198803201"}
SRC_FRENKEL = {"source_type": "external_doc", "title": "Frenkel & Smit, Understanding Molecular Simulation",
               "locator": "isbn:9780122673511"}
SRC_STUK = {"source_type": "literature", "title": "Stukowski, MSMSE 18, 015012 (2010) OVITO",
            "locator": "doi:10.1088/0965-0393/18/1/015012"}
SRC_EST = {"source_type": "estimate", "title": "量级估算", "locator": BATCH + "/estimate"}


def bench(path, title):
    return {"source_type": "run_artifact", "title": title, "locator": "automd-microbench-mini/" + path}


def T(facet, tag):
    return {"facet": facet, "tag": tag}


D = T("domain", "metal-md")
LMP = T("tool", "lammps")


def lesson(title, situation, risk, fix, check, tags, sources, conditions=None, origin=None):
    return {"type_id": "lesson", "title": title,
            "fields": {"situation": situation, "risk": risk, "fix": fix, "check": check},
            "tags": [D] + tags, "sources": sources, "conditions": conditions or [],
            "origin": origin or ORIGIN_TEXTBOOK}


def rule(title, statement, severity, rationale, check, tags, sources, exceptions=None):
    f = {"statement": statement, "severity": severity, "rationale": rationale, "check": check}
    if exceptions:
        f["exceptions"] = exceptions
    return {"type_id": "rule", "title": title, "fields": f, "tags": [D] + tags, "sources": sources,
            "origin": ORIGIN_TEXTBOOK}


def param(title, quantity, unit, tags, sources, value=None, lo=None, hi=None, method=None, note=None,
          conditions=None, origin=None):
    f = {"quantity": quantity, "unit": unit}
    for k, v in (("value", value), ("min", lo), ("max", hi), ("method", method), ("note", note)):
        if v is not None:
            f[k] = v
    return {"type_id": "parameter", "title": title, "fields": f, "tags": [D] + tags, "sources": sources,
            "conditions": conditions or [], "origin": origin or ORIGIN_TEXTBOOK}


def failure(title, signature, symptoms, causes, fixes, tags, sources, origin=None):
    return {"type_id": "failure_pattern", "title": title,
            "fields": {"signature": signature, "symptoms": symptoms, "causes": causes, "fixes": fixes},
            "tags": [D, LMP, T("topic", "run-failure")] + tags, "sources": sources, "origin": origin or ORIGIN_BENCH}


def proc(title, goal, steps, tags, sources, pre=None, pitfalls=None):
    f = {"goal": goal, "steps": steps}
    if pre:
        f["preconditions"] = pre
    if pitfalls:
        f["pitfalls"] = pitfalls
    return {"type_id": "procedure", "title": title, "fields": f, "tags": [D] + tags, "sources": sources,
            "origin": ORIGIN_TEXTBOOK}


ENTRIES = [
    # ---------------- potentials ----------------
    lesson("funcfl 单元素 EAM 文件 pair_coeff 不能带元素名",
           "pair_style eam 读取 funcfl 格式（如 Cu_u3.eam）时写成 pair_coeff * * Cu_u3.eam Cu。",
           "LAMMPS 报 ERROR: Incorrect args for pair coefficients，运行在建模后立即中止。",
           "funcfl 格式写 pair_coeff * * Cu_u3.eam，不带元素名；带元素映射的是 eam/alloy 的 setfl 文件。",
           "日志出现 Reading eam potential file 且能进入 run。",
           [LMP, T("topic", "potential-format"), T("object", "fcc-metal")],
           [bench("runs/md03-cu-tension/20260929-202456/ws/submission/artifacts/attempt1_failed_log.lammps",
                  "拉伸任务首次失败日志")], origin=ORIGIN_BENCH),
    rule("eam/alloy 的元素映射顺序必须与 atom type 一一对应",
         "pair_coeff * * file.setfl A B 中第 i 个元素名对应 type i；顺序写错不会报错但体系元素被调换。",
         "hard", "setfl 文件可包含多种元素，LAMMPS 按位置映射 type，错误映射得到的是另一个合金。",
         "建模后打印各 type 质量与元素名核对，或 run 0 检查内聚能与预期相符。",
         [LMP, T("topic", "potential-format"), T("object", "alloy")], [SRC_DOC]),
    lesson("使用势函数前先复现其 0 K 晶格常数",
           "直接拿来一个 EAM 势就开始做性质计算，初始晶格常数沿用实验值。",
           "势的平衡晶格常数与实验值差 0.5%~2% 很常见，用错初始晶格会引入残余应力，NPT 下要额外弛豫，NVT 下结果有偏。",
           "先做 0 K 晶格扫描或 box/relax 得到该势的 a0，再用它建模。",
           "minimize 后压力接近 0（|P| < 几十 bar），且与势文献报告的 a0 一致。",
           [LMP, T("topic", "potential-validation"), T("method", "energy-minimization"),
            T("property", "lattice-density")],
           [bench("runs/md01-cu-npt/20260929-190339/ws/run/in.npt", "NPT 输入注释：a0 由 in.min 得到"), SRC_FOILES]),
    param("Cu_u3.eam（Foiles 1986）0 K 晶格常数", "fcc Cu 0 K 平衡晶格常数", "Å",
          [T("topic", "potential-validation"), T("object", "fcc-metal"), T("property", "lattice-density")],
          [bench("suite/md01-cu-npt/reference/reference.json", "评测参考（ASE 0 K 静态）"), SRC_FOILES],
          value=3.615, method="ASE 纯 Python EAM 0 K 静态与 LAMMPS minimize 互证",
          conditions=[{"key": "potential_family", "value": "eam"}, {"key": "crystal_structure", "value": "fcc"}],
          origin=ORIGIN_BENCH),
    rule("势函数的适用范围以拟合数据为界",
         "势函数只在其拟合覆盖的结构、温度、压力与元素组成范围内可信；超出范围的结论必须单独验证。",
         "soft", "EAM 等经验势对未拟合的相（如高压相、表面重构、液态高温区）误差可能很大。",
         "确认势文献的拟合集，对关键量（层错能、熔点、表面能）做抽样比对。",
         [T("topic", "potential-validation")], [SRC_MISHIN],
         exceptions="机器学习势同样适用此规则，且外推更难察觉。"),
    lesson("EAM 不能描述金属的电子热导",
           "用 Müller-Plathe 或 Green-Kubo 在 EAM 金属中算热导率并与实验对比。",
           "经典 MD 只给晶格（声子）热导，金属热导以电子为主，结果偏低一个量级左右。",
           "明确报告为晶格热导；需要总热导时另做电子贡献（如 Wiedemann-Franz 估计或双温模型）。",
           "报告中写明只含声子贡献。",
           [T("topic", "thermal"), T("method", "nemd"), T("property", "thermal-conductivity"),
            T("object", "fcc-metal")], [SRC_TEXT]),

    # ---------------- integration ----------------
    rule("金属 EAM 体系时间步长默认 1 fs",
         "metal 单位下 EAM 金属常用 timestep 0.001 ps；高温、轻元素或强冲击时应减小到 0.5 fs 或更小。",
         "soft", "原子振动周期约 0.1 ps 量级，1 fs 能保证能量守恒；冲击和高温下原子速度大，需更小步长。",
         "NVE 短测 10 ps，总能量相对漂移小于 1e-4。",
         [LMP, T("topic", "integration"), T("object", "fcc-metal")],
         [bench("runs/md01-cu-npt/20260929-190339/ws/run/in.npt", "NPT 输入 timestep 0.001"), SRC_TEXT]),
    param("LJ 约化单位下稳定时间步长", "dt*", "τ（约化时间）",
          [T("topic", "integration"), T("object", "lj-model")],
          [bench("suite/md16r-restart-recover/reference/reference.json", "重启任务参考：dt*<=0.005"), SRC_FRENKEL],
          hi=0.005, method="T*≈1 时常用 0.002~0.005；T* 越高需越小",
          conditions=[{"key": "units_style", "value": "lj"}], origin=ORIGIN_BENCH),
    failure("Lost atoms：步长过大或初速过高导致积分发散",
            "ERROR: Lost atoms: original N current M",
            ["运行数十步后中止", "中止前温度、势能剧烈上升", "部分原子飞出盒子"],
            ["timestep 相对体系温度过大（例：LJ 体系 dt*=0.025 + T*=5.0）", "初始构型原子重叠", "势函数截断处不连续"],
            ["减小 timestep（LJ 体系到 0.005 以下）", "降低初始温度或先 minimize", "检查构型最近邻距离",
             "必要时 thermo_modify lost warn 定位，但不要用它掩盖问题"],
            [T("topic", "integration"), T("object", "lj-model")],
            [bench("suite/l0d-log-diagnosis/assets/log_A.txt", "故障日志 A"),
             bench("runs/md16r-restart-recover/20260929-215801/ws/submission/scientific_report.md", "重启任务诊断报告")]),
    lesson("NVE 能量漂移是检查步长的第一手证据",
           "改了势函数或体系后直接上 NPT/NVT 生产，没有单独验证积分。",
           "恒温器会掩盖积分误差：温度看着正常，能量实际在被热浴持续修正。",
           "生产前跑一段 NVE，看 etotal 漂移；相对漂移超过 1e-4/ns 量级就减小步长。",
           "记录 NVE 段 etotal 首末差与均值之比。",
           [LMP, T("topic", "integration"), T("topic", "equilibration")],
           [bench("suite/md16r-restart-recover/reference/reference.json", "参考：完整 NVE 漂移 3.0e-3"), SRC_TEXT]),

    # ---------------- ensembles ----------------
    param("Nose-Hoover 恒温阻尼 Tdamp", "Tdamp", "时间步数",
          [LMP, T("topic", "ensemble-control")], [SRC_DOC], value=100,
          note="LAMMPS 文档建议约 100 个时间步；metal 单位 dt=1 fs 时为 0.1 ps"),
    param("Nose-Hoover 恒压阻尼 Pdamp", "Pdamp", "时间步数",
          [LMP, T("topic", "ensemble-control")], [SRC_DOC], value=1000,
          note="约 1000 个时间步；过小会导致体积/密度剧烈振荡"),
    failure("恒压阻尼过小导致密度持续振荡",
            "NPT 运行中 density/vol 大幅周期性振荡且不收敛",
            ["密度在 0.68~0.91（LJ 约化）之间来回", "压力瞬时值极大"],
            ["Pdamp 只有几个时间步（例：Pdamp=0.03，dt=0.005）"],
            ["Pdamp 改为约 1000*dt", "先 NVT 平衡温度再开 NPT"],
            [T("topic", "ensemble-control"), T("object", "lj-model")],
            [bench("suite/l0d-log-diagnosis/assets/log_B.txt", "故障日志 B")]),
    lesson("平衡判据用前后半窗均值比较，不只看温度",
           "只看温度达到目标就认为平衡，开始统计。",
           "温度最快平衡，体积、势能、结构量可能仍在漂移，统计结果带系统偏差。",
           "把候选生产段分两半，关注量的两半均值差应小于一个标准差；或做块平均看是否平稳。",
           "报告中列出两半窗均值与标准差。",
           [T("topic", "equilibration"), T("topic", "reporting")],
           [bench("suite/md01-cu-npt/prompt/natural.md", "NPT 任务平衡判据要求")], origin=ORIGIN_BENCH),
    rule("拉伸前用 NPT 弛豫到零应力，再切换到 NVT+deform",
         "单轴加载前先在目标温度 NPT（P=0）平衡，记录加载起点盒长作为应变参考，再 unfix 换成 NVT + fix deform。",
         "hard", "未弛豫的残余应力会叠加在加载曲线上，应变参考取错会使模量和峰值应变系统偏移。",
         "加载起点 pxx、pyy、pzz 均接近 0；应变用加载起点盒长计算。",
         [LMP, T("topic", "mechanical-loading"), T("topic", "equilibration")],
         [bench("runs/md03-cu-tension/20260929-202456/ws/in.tension", "拉伸输入 Stage 1/2")]),
    lesson("各向异性加载时用 aniso 或分量控压",
           "做单轴应力（横向自由）加载时仍用 fix npt iso 或横向固定盒长。",
           "iso 会耦合三个方向；横向固定得到的是单轴应变条件，测出的是约束模量而不是 Young 模量。",
           "单轴应力：fix npt 只对 y、z 设 0 压，x 方向 deform；或 aniso 并分别指定。",
           "加载过程中横向应力接近 0。",
           [LMP, T("topic", "mechanical-loading"), T("topic", "ensemble-control"), T("property", "elastic")],
           [bench("runs/md03-cu-tension/20260929-202456/ws/submission/scientific_report.md",
                  "拉伸报告局限 2：横向受限")]),

    # ---------------- mechanical ----------------
    lesson("MD 应变率比实验高 10 个数量级以上，峰值应力偏高",
           "用 erate 0.005/ps（5e9/s）拉伸单晶得到峰值应力 12~13 GPa，直接与实验屈服强度比较。",
           "位错形核是热激活过程，超高应变率使形核推迟，强度系统偏高；完美单晶又缺少位错源。",
           "报告为高应变率下的理想强度上限；需要比较时做多个应变率外推，或引入预存缺陷。",
           "报告中写明应变率与实验差多少个数量级。",
           [T("topic", "strain-rate"), T("property", "strength"), T("object", "fcc-metal")],
           [bench("runs/md03-cu-tension/20260929-202456/ws/submission/scientific_report.md", "拉伸报告局限 1")],
           conditions=[{"key": "strain_rate_per_ps", "value": 0.005}], origin=ORIGIN_BENCH),
    rule("LAMMPS 压力与拉应力符号相反",
         "metal 单位下应力 σxx（拉为正，GPa）= -pxx × 1e-4；pxx 单位为 bar。",
         "hard", "LAMMPS 压力张量以压为正；不换号会把拉伸曲线画成负值。",
         "小应变拉伸时 σxx 为正且随应变增加。",
         [LMP, T("topic", "stress-calc")],
         [bench("runs/md03-cu-tension/20260929-202456/ws/in.tension", "拉伸输入 sxx = -pxx*0.0001")]),
    lesson("单原子维里应力需要除以原子体积才是应力",
           "compute stress/atom 的结果直接当作每个原子的应力画图。",
           "stress/atom 输出的是应力×体积（bar·Å³），不除体积量纲错误、数值差几个数量级。",
           "用 Voronoi 体积或平均原子体积除；对整体求和再除总体积可回到压力张量。",
           "整体求和 / 体积 与 thermo 的 pxx 一致。",
           [LMP, T("topic", "stress-calc"), T("topic", "structure-analysis")], [SRC_DOC]),
    param("fcc Cu（Cu_u3.eam）300 K [100] 单轴应变拉伸弹性段斜率", "dσxx/dε（单轴应变）", "GPa",
          [T("topic", "mechanical-loading"), T("property", "elastic"), T("object", "fcc-metal")],
          [bench("suite/md03-cu-tension/reference/reference.json", "评测参考"),
           bench("runs/md03-cu-tension/20260929-202456/ws/submission/final_answer.json", "被测运行结果")],
          value=154.0, lo=143.0, hi=164.0,
          method="5x5x12 超胞，NPT 预平衡后 NVT+deform erate 0.005/ps，拟合 ε∈[0.002,0.02]",
          note="拟合窗口选择带来约 ±6 GPa 系统散布；对应 300 K 有效 C11，不是 Young 模量",
          conditions=[{"key": "temperature_k", "value": 300}, {"key": "strain_rate_per_ps", "value": 0.005},
                      {"key": "potential_family", "value": "eam"}], origin=ORIGIN_BENCH),
    proc("0 K 能量-应变法计算立方晶体弹性常数",
         "得到 C11、C12、C44 并检查 Born 稳定性。",
         ["minimize 得到该势的 a0，盒子转 triclinic。",
          "三种应变模式各取 ±0.002~±0.010 共 11 点：静水 diag(1+d)、四方 diag(1+d,1-d,1)、三斜剪切非对角 d。",
          "每点 run 0 记录 pe/atom 与 vol/atom。",
          "拟合 ΔE/V0 = a d²：静水 a=(3/2)(C11+2C12)，四方 a=C11-C12，剪切 a=(3/2)C44。",
          "换算单位（eV/Å³ → GPa 乘 160.2）并检查 C11>|C12|、C44>0、C11+2C12>0。"],
         [LMP, T("topic", "potential-validation"), T("method", "energy-strain"), T("property", "elastic"),
          T("object", "fcc-metal")],
         [bench("runs/md02-cu-elastic/20260929-222439/ws/scripts/elastic.in", "弹性常数脚本")],
         pre=["0 K", "完美晶体"],
         pitfalls=["clear 会重置 units，循环内要重新设置", "change_box 默认 lattice 单位，倾斜量要换算",
                   "应变过大进入非谐区，过小被截断噪声主导"]),
    param("Cu_u3.eam 0 K 弹性常数 C11/C12/C44", "C11, C12, C44", "GPa",
          [T("method", "energy-strain"), T("property", "elastic"), T("object", "fcc-metal"),
           T("topic", "potential-validation")],
          [bench("suite/md02-cu-elastic/reference/reference.json", "评测双实现参考"),
           bench("runs/md02-cu-elastic/20260929-222439/ws/submission/final_answer.json", "被测运行")],
          method="能量-应变法；LAMMPS 与 ASE 互证 C11-C12=43.1、C44=76.8",
          note="参考 C11=163.6, C12=120.5, C44=76.8；被测运行 167.3/124.2/76.4，在 ±6% 内",
          conditions=[{"key": "temperature_k", "value": 0}, {"key": "potential_family", "value": "eam"}],
          origin=ORIGIN_BENCH),

    # ---------------- model building / defects ----------------
    lesson("Voronoi 多晶建模后必须删除重叠原子",
           "用 Atomsk 或自写脚本 Voronoi 方法生成多晶后直接开跑。",
           "晶界处原子距离过近，初始力极大，导致 Lost atoms 或局部熔化。",
           "delete_atoms overlap（截断约 0.7×最近邻距离）后 minimize，再逐步升温。",
           "minimize 后最大原子力小于 1e-2 eV/Å，且无局部无序区异常扩大。",
           [T("topic", "polycrystal"), T("tool", "atomsk"), LMP], [SRC_DOC]),
    rule("晶粒尺寸至少要容纳若干个位错间距",
         "多晶模拟中每个晶粒的尺寸应远大于位错芯与层错宽度，常用 ≥10 nm；过小的晶粒行为由晶界主导。",
         "soft", "纳米晶强度存在反 Hall-Petch 区，小晶粒结果不能外推到常规晶粒。",
         "报告平均晶粒尺寸与晶粒数，必要时做尺寸对比。",
         [T("topic", "polycrystal"), T("topic", "size-effects"), T("property", "strength")], [SRC_EST]),
    lesson("周期边界下位错与自身像相互作用",
           "在小盒子里插入单根位错计算 Peierls 应力或迁移率。",
           "周期像产生的长程应力场改变位错受力，盒子小时误差显著。",
           "沿位错线方向保持周期，垂直方向用足够大的盒子或固定边界层，并做尺寸收敛。",
           "两种盒子尺寸下关注量变化小于 5%。",
           [T("topic", "defects"), T("topic", "size-effects")], [SRC_EST]),
    param("fcc Cu 层错能（实验量级）", "固有层错能 γ_isf", "mJ/m²",
          [T("topic", "defects"), T("property", "defect-energy"), T("object", "fcc-metal")],
          [SRC_MISHIN], lo=40, hi=80, note="不同 EAM 势差异大（约 20~70）；选势时应比对"),
    proc("空位形成能计算",
         "得到 E_f = E(N-1) - (N-1)/N · E(N)。",
         ["完美超胞 minimize 记录 E(N)。", "删除一个原子后 minimize（可含 box/relax）记录 E(N-1)。",
          "按公式计算，检查超胞尺寸收敛（至少 500 原子以上对比）。"],
         [LMP, T("topic", "defects"), T("property", "defect-energy"), T("method", "energy-minimization")],
         [SRC_TEXT], pitfalls=["忘记 (N-1)/N 因子会把内聚能混进去", "minimize 收敛阈值过松"]),
    lesson("CNA 识别前用自适应截断",
           "用固定截断 CNA 分析高温或强变形体系的结构。",
           "热振动使近邻距离展宽，固定截断把大量 fcc 原子识别为 other，层错比例被高估或低估。",
           "用 adaptive CNA 或 PTM；高温下可先对短时间轨迹做时间平均再识别。",
           "完美晶体在目标温度下识别为 fcc 的比例接近 100%。",
           [T("topic", "structure-analysis"), T("tool", "ovito"), T("topic", "defects")], [SRC_STUK]),
    lesson("DXA 位错分析需要足够的晶体参考结构",
           "对高度无序或大部分非晶化的区域运行 DXA，得到大量零碎位错段。",
           "DXA 依赖局部晶格识别，严重无序区无法构建 Burgers 回路，结果不可信。",
           "先用结构识别确认晶体比例，把无序区作为缺陷网格单独报告。",
           "位错总长度随时间变化连续，无大量 <1 nm 碎段。",
           [T("topic", "structure-analysis"), T("tool", "ovito"), T("topic", "defects")], [SRC_STUK]),

    # ---------------- thermal / diffusion ----------------
    lesson("单相加热得到的熔点偏高（过热）",
           "从完美晶体线性升温，以势能跃变的温度作为熔点。",
           "无表面或缺陷的完美晶体会过热 20% 左右，测得的是机械失稳温度而不是热力学熔点。",
           "用两相共存法（固液界面），或含自由表面/空洞的样品加热。",
           "共存模拟中界面在若干百 ps 内不移动的温度即熔点。",
           [T("topic", "thermal"), T("method", "two-phase"), T("property", "melting")], [SRC_FRENKEL]),
    param("Cu_u3.eam 300 K、0 bar 平衡密度与晶格常数", "密度 / 晶格常数", "g/cm³ / Å",
          [T("topic", "equilibration"), T("property", "lattice-density"), T("object", "fcc-metal")],
          [bench("suite/md01-cu-npt/reference/reference.json", "评测参考"),
           bench("runs/md01-cu-npt/20260929-190339/ws/submission/final_answer.json", "被测运行")],
          value=8.8053, method="NPT 300 K/0 bar，864 原子，Tdamp 0.1 ps、Pdamp 1.0 ps，平衡 6000 + 生产 6000 步",
          note="晶格常数 3.6327 Å；运行内密度涨落 σ≈0.015~0.018",
          conditions=[{"key": "temperature_k", "value": 300}, {"key": "pressure_bar", "value": 0}],
          origin=ORIGIN_BENCH),
    lesson("扩散系数要用线性段 MSD 并做 unwrap",
           "直接用 dump 中的折回坐标算 MSD，或把起始弹道段一起拟合。",
           "折回坐标使 MSD 饱和在盒长平方量级；弹道段斜率不是扩散。",
           "用 compute msd（自动 unwrap）或输出 image flag；只拟合长时间线性段，D = 斜率/6。",
           "log-log 图中拟合段斜率接近 1。",
           [T("topic", "diffusion"), T("property", "diffusivity")], [SRC_TEXT]),
    lesson("固态金属自扩散在 MD 时间尺度内几乎测不到",
           "在 0.7Tm 以下的完美晶体中算自扩散系数。",
           "无空位时扩散依赖空位形成，ns 尺度内几乎没有跳跃事件，MSD 只有热振动平台。",
           "引入空位并按空位浓度换算，或用 kMC/加速动力学；只在接近熔点或液态时直接 MD。",
           "MSD 平台值约等于原子振幅平方说明没有扩散事件。",
           [T("topic", "diffusion"), T("property", "diffusivity"), T("object", "fcc-metal")], [SRC_EST]),

    # ---------------- restart / performance / reporting ----------------
    proc("断点续算并验证连续性",
         "让长任务可中断续跑，且续算结果与连续跑一致。",
         ["主运行中用 restart N restart.* 定期写出。",
          "续算输入 read_restart restart.4000 后重新定义 fix、thermo（restart 文件不保存 fix 的全部状态）。",
          "不要 reset_timestep，步号由 restart 文件带入。",
          "对比续算段与连续运行在接缝步的 etotal 与温度，相对差应在 1e-3 以下。"],
         [LMP, T("topic", "restart")],
         [bench("runs/md16r-restart-recover/20260929-215801/ws/run2/in_restart.in", "续算输入"),
          bench("suite/md16r-restart-recover/reference/reference.json", "参考：接缝相对差 0.00")],
         pitfalls=["restart 文件与 LAMMPS 版本、编译选项绑定", "Nose-Hoover 链状态需要 fix 写入 restart 才能连续"]),
    lesson("邻居列表 skin 过小会导致频繁重建或丢失相互作用",
           "metal 单位下 neighbor 0.3 bin（LJ 常用值）直接用于高温金属。",
           "原子一步移动超过 skin/2 时可能漏算相互作用；check yes 会频繁重建拖慢速度。",
           "metal 单位常用 neighbor 2.0 bin + neigh_modify delay 0 every 1 check yes。",
           "日志 Dangerous builds = 0。",
           [LMP, T("topic", "performance"), T("topic", "integration")],
           [bench("runs/md01-cu-npt/20260929-190339/ws/run/in.npt", "NPT 输入 neighbor 2.0")]),
    rule("报告中所有数值必须来自本次运行产物",
         "结果数字只能来自本次运行的日志或数据文件，并注明文件与位置；不能用文献值或记忆值充当计算结果。",
         "hard", "可追溯是复核的前提；混入外部数值会让错误无法被发现。",
         "每个结果数字旁都有文件路径与行号或数据列说明。",
         [T("topic", "reporting")], [bench("suite/md01-cu-npt/prompt/natural.md", "任务执行纪律")]),
    lesson("单次运行的不确定度要区分统计误差与方法散布",
           "只报告拟合标准误作为不确定度。",
           "拟合窗口、种子、尺寸等方法选择带来的散布常比统计误差大一个量级（如拉伸斜率：统计 0.7 GPa，窗口散布约 6 GPa）。",
           "同时报告统计误差与关键方法选择的敏感性；有条件时多种子。",
           "不确定度表中至少一项方法敏感性。",
           [T("topic", "reporting"), T("topic", "mechanical-loading")],
           [bench("runs/md03-cu-tension/20260929-202456/ws/submission/final_answer.json", "拉伸结果不确定度")],
           origin=ORIGIN_BENCH),

    # ---------------- definitions / faq / checklist / correction / template ----------------
    {"type_id": "definition", "title": "EAM（嵌入原子法）",
     "fields": {"term": "EAM", "definition": "嵌入原子势：总能量 = 对势项 + 每个原子嵌入到周围电子密度中的嵌入能 F(ρ)。适合描述金属键的多体特性。",
                "aliases": ["embedded atom method", "嵌入原子势"]},
     "tags": [D, T("topic", "potential")], "origin": ORIGIN_TEXTBOOK},
    {"type_id": "definition", "title": "Born 机械稳定性判据（立方晶体）",
     "fields": {"term": "Born 稳定性", "definition": "立方晶体弹性稳定的充要条件：C11 - C12 > 0，C44 > 0，C11 + 2C12 > 0。",
                "aliases": ["Born criteria"]},
     "tags": [D, T("topic", "mechanical-loading"), T("property", "elastic")], "origin": ORIGIN_TEXTBOOK},
    {"type_id": "faq", "title": "NVT 和 NPT 选哪个做平衡？",
     "fields": {"question": "固体性质计算前的平衡用 NVT 还是 NPT？",
                "answer": "目标是零压/给定压力下的性质时先 NPT 让体积弛豫，再按需要切 NVT 采样；若已确知该温度下的平衡体积，可直接 NVT。拉伸等变形前必须先 NPT 到零应力。"},
     "tags": [D, T("topic", "ensemble-control")], "sources": [SRC_TEXT], "origin": ORIGIN_TEXTBOOK},
    {"type_id": "checklist", "title": "金属 MD 输入脚本自查清单",
     "fields": {"purpose": "运行前逐项核对输入脚本，避免常见低级错误。",
                "items": [{"question": "units 与势函数要求一致？", "pass_criteria": "EAM/MEAM 用 metal"},
                          {"question": "pair_coeff 写法与势文件格式一致？", "pass_criteria": "funcfl 不带元素名；setfl 按 type 顺序映射"},
                          {"question": "初始晶格常数来自该势？", "pass_criteria": "注释写明来源"},
                          {"question": "timestep 合理？", "pass_criteria": "metal 默认 0.001 ps"},
                          {"question": "恒温恒压阻尼合理？", "pass_criteria": "Tdamp≈100dt，Pdamp≈1000dt"},
                          {"question": "随机种子已记录？", "pass_criteria": "velocity 种子写入 manifest"}]},
     "tags": [D, LMP, T("topic", "run-failure"), T("stage", "setup")], "sources": [SRC_DOC],
     "origin": ORIGIN_TEXTBOOK},
    {"type_id": "correction", "title": "纠正：stress/atom 输出不是应力",
     "fields": {"wrong": "compute stress/atom 的每原子输出就是该原子的应力（bar）。",
                "right": "输出是应力×体积（bar·Å³），需要除以原子体积。",
                "why": "LAMMPS 文档明确该量的单位是 pressure*volume。"},
     "tags": [D, LMP, T("topic", "stress-calc")], "sources": [SRC_DOC], "origin": ORIGIN_TEXTBOOK},
    {"type_id": "template", "title": "单轴拉伸应力应变输出片段",
     "fields": {"usage": "NVT + deform 阶段输出工程应变与拉应力（GPa）。",
                "content": "variable Lx0 equal $(lx)\nvariable epsx equal (lx-v_Lx0)/v_Lx0\nvariable sxx equal -pxx*0.0001\nfix curve all print 20 \"${epsx} ${sxx}\" file sigma_eps.txt screen no",
                "placeholders": ["输出间隔 20", "文件名 sigma_eps.txt"]},
     "tags": [D, LMP, T("topic", "mechanical-loading"), T("topic", "stress-calc")], "origin": ORIGIN_TEXTBOOK},
    {"type_id": "claim", "title": "EAM 势预测的 Cu 弹性常数普遍高于实验 C12",
     "fields": {"statement": "多数 Cu EAM 势拟合到 C11、C44 后，C12 偏高，导致 Cauchy 压力偏大。",
                "scope_note": "针对常用 Cu EAM（Foiles、Mishin 等），未覆盖所有势。",
                "evidence_note": "Cu_u3 的 C12=120.5 GPa，实验约 121~124，偏差不大；本条为待核实断言。"},
     "tags": [D, T("topic", "potential-validation"), T("property", "elastic"), T("object", "fcc-metal")],
     "sources": [SRC_MISHIN, bench("suite/md02-cu-elastic/reference/reference.json", "评测参考")],
     "origin": ORIGIN_TEXTBOOK},
    {"type_id": "observation", "title": "Cu 单晶 [100] 拉伸至 ε=0.11 未出现屈服",
     "fields": {"statement": "300 K、erate 0.005/ps、单轴应变条件下拉伸到 ε=0.11，σxx 仍单调上升至 12.68 GPa，未观察到应力跌落。",
                "observed_at": "2026-09-29", "context": "automd microbench md03，1200 原子完美单晶"},
     "tags": [D, T("topic", "mechanical-loading"), T("property", "strength"), T("object", "fcc-metal")],
     "sources": [bench("runs/md03-cu-tension/20260929-202456/ws/submission/final_answer.json", "拉伸结果")],
     "origin": ORIGIN_BENCH},
    lesson("bcc 金属位错芯需要能描述非平面芯结构的势",
           "用简单 EAM 势模拟 bcc Fe/W 的螺位错滑移。",
           "部分 EAM 势给出简并芯结构，导致 Peierls 势垒与滑移面选择错误。",
           "选用经芯结构验证的势（如部分 Fe 的 Mendelev 势、W 的机器学习势），并与 DFT 芯结构比对。",
           "螺位错芯为非简并结构，Peierls 势垒量级与 DFT 一致。",
           [T("topic", "defects"), T("topic", "potential-validation"), T("object", "bcc-metal")], [SRC_EST]),
    lesson("hcp 金属建模要注意 c/a 与取向",
           "按理想 c/a=1.633 建 hcp Ti/Mg 超胞，加载方向随意。",
           "实际 c/a 偏离理想值，未弛豫产生残余应力；hcp 力学强烈依赖加载方向相对 c 轴。",
           "用该势弛豫后的 a、c 建模；明确加载方向（a 轴、c 轴）并报告。",
           "弛豫后各方向压力接近 0。",
           [T("topic", "model-building"), T("object", "hcp-metal")], [SRC_EST]),
    lesson("高熵合金无序构型需要多个随机样本",
           "只生成一个随机无序 HEA 构型就计算性质。",
           "局部化学环境的随机性使单个样本结果有显著散布，尤其是缺陷能与局部力学性质。",
           "生成至少 3~5 个随机样本取平均与标准差；必要时用 MC/MD 考虑短程有序。",
           "报告样本数与样本间标准差。",
           [T("object", "alloy"), T("topic", "model-building"), T("topic", "reporting")], [SRC_EST]),
    lesson("GPU 包加速对 EAM 收益依赖体系规模",
           "几千原子的小体系开 GPU 包期望大幅加速。",
           "小体系 GPU 利用率低，数据传输开销占主导，可能比 CPU 还慢。",
           "十万原子以上再考虑 GPU；小体系优先 MPI 多核与 OpenMP。",
           "对比 Performance 行的 timesteps/s。",
           [LMP, T("topic", "performance")], [SRC_DOC]),
]
