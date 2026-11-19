# review_experience —— 审阅经验注入库(给 AI 的知识,不是给代码执行的规则)

> 架构定位(2026-10-04 定稿):**代码度量、AI 判断、经验供智**。
> physics_gate 只测量(dmin/电荷/衍生比值/结构存在性),design_review 的 AI 审阅做全部判断,
> 本库是 AI 审阅的知识来源——注入经验=给 AI 知识,新经验落盘即生效,**永不改代码**。
> 这就是"让 AI 持续进化、知道什么能做什么不能做"的通道。

## 目录与加载

- **种子库**:`mcp/review_experience/*.json`(git 版本化,人工策展)
- **运行时注入库**:`<数据根>/memory/review_experience/`(env `REVIEW_EXPERIENCE_DIR` 可覆盖),
  演化层/人工直接落盘
- 每次 `design_review.run()` 热加载(mtime+size 缓存);坏包/坏条目**隔离进报告**(`experience.quarantined`),
  绝不打崩审阅
- 条目按 intent 关键词/实验类型/conditions 匹配后注入审阅简报;AI 引用须回指条目 id,
  仲裁层核验——伪造引用=unverified 且降级为 soft

## 共享目录与包归属(2026-10-17)

本目录同时存放**证据系统(evidence-v2)的包**:`anchors_seed.json`(physics-anchor 锚点,
`evolution/assessment.py` 消费)与 `methodology_deep.json`(methodology-dimension 审阅维度,
`evolution/deep_review.py` 消费)。它们的条目是各自 schema(无 statement),design_review
按包级 `"consumer"` 字段整包跳过——**外来包必须显式声明 `"consumer": "evidence"`**,
未声明即视为本流水线的包、条目必须全过校验(自检测试 `TestSeedPackIntegrity` 强制)。
`methodology_seed.json` 是共享包(无 consumer 标记):条目带 statement + machine_gate,
运行期由 `evolution/config_check.py` 机判,设计期由本流水线 AI 审(kind=methodology,2026-10-17
收编进 kind 词表——如"各向同性 NPT 与变形互斥"在 intent.ensemble_sequence 层即可审)。

## 经验条目格式(knowledge 包)

```jsonc
{
  "pack": "my_pack", "schema_version": 1,
  "entries": [
    {
      "id": "exp-xxx-001",                    // 包内唯一;建议 exp-<域>-<序号>
      "kind": "boundary|lesson|convention|capability|completeness|methodology",
      "statement": "一句话经验(可含数值边界与判据)",   // 给 AI 读的核心
      "conditions": {"engine": "LAMMPS", "family": "friction", "aspect": "..."},  // 匹配面
      "severity_hint": "hard|soft",           // AI 判定严重度的参考,非强制
      "rationale": "为什么(实测事故/文献/机理)",
      "remediation": ["整改选项"],
      "refs": ["LEDGER:...", "atom:a17...", "paper:doi"],   // 溯源
      "provenance": {"source": "human|evolution|literature|seed", "injected_by": "...", "ts": "..."},
      "status": "active|canary|revoked",
      "exemptible": false                     // 仅真正物理正当的例外(如续跑/金属短键)可 true
    }
  ]
}
```

- `status` 生命周期:`active` 生效;`canary`=影子模式(AI 可引用但报告标注"试验性经验",
  不得单独作为 hard 依据——新注入经验的降落伞);`revoked`=停用(保留溯源,不删除)。
- `severity_hint: hard` 的条目被 AI 采信为 hard finding 时,引用核验不通过会被仲裁层
  自动降级 soft——**经验的效力由证据支撑,不由声明决定**。

## 完整性清单格式(completeness 包)

```jsonc
{"pack": "...", "schema_version": 1, "kind": "completeness",
 "family": "*|friction|...",                 // 按 experiment_type 子串匹配
 "required_fields": ["key_params.timestep_fs", ...],   // 代码存在性扫描,缺=hard 事实
 "guidance": [{"aspect": "...", "why": "...", "proposal": "典型补法"}]}  // AI 补全建议素材
```

## 校验与隔离

- 包级:JSON 可解析、schema_version=1、entries/required_fields 为 list
- 条目级:id 非空且 `[A-Za-z0-9_\-]`、statement 非空、severity_hint 枚举、status 枚举
- 违规条目剔除并记 `experience.quarantined`(带原因),合规条目正常生效

## 与演化层的对接(预留)

后续把本目录注册为 apply_proposal 的 evidence 类(单写者+LEDGER 审计+回放晋升),
字段已按其惯例预留:provenance 三件套/refs 溯源/status 生命周期(等价 canary→promote)。
人工注入:直接放文件;机器注入:待演化通道接入后走提案。

## 种子内容(等价迁移自原硬编码门 + 实验-仿真一致性扩充,2026-10-04)

- physics_basics.json:15 条(H1-H5/C1-C4/S1-S7 知识)
- experiment_design.json:8 条(E0-E8 知识,含摩擦族)
- **material_structure.json:10 条(B 层材料模型保真:固溶随机化/晶格口径/单晶近似/晶界/非晶/交联/表面/缺陷/尺寸代表性/声明-产物一致)**
- **experiment_abstraction.json:8 条(C 层抽象映射:接触换算适用条件/速率外推/时间桥接/样品尺度/环境省略/不确定度/粗糙度/映射依据底线)**
- **comparability.json:8 条(H/A 层:可直接比清单/代理量设计/趋势对照/为了仿真而仿真/覆盖完备性/噪声地板/不可比处理/声明-能力匹配)**
- completeness_generic.json / completeness_friction.json:完整性清单与补全素材
- **completeness_paper_grade.json:论文型四段必填清单(min_grade 路由:exploratory 不生效)**

## 审阅维度(八层,A-I;findings 带 layer 字段,报告按层归组)

A 目标与解释角色 / B 材料模型保真 / C 抽象映射保真 / D 物理可表达性与尺度 /
E 设定自洽 / F 数值与有限尺度 / G 统计与收敛 / H 对照协议与可比性 / I 完整性(横切)。
一致性总原则:**不问"能否复刻实验数值",只问"在仿真合理的条件下,这份设计能否对实验给出可辩护的解释"**。

## 严重度三档(仲裁纪律,2026-10-04)

- **hard**=判定为错(须真凭据:经验条目 id/$事实名/结构;无凭据自动降 uncertain)
- **uncertain**=判不了(信息缺/依据不足;必须消解:改设计/补证据/用户裁定)——**阻断放行**
- **soft**=风险/惯例偏离(canary 试验性经验只能撑到这档)
- 放行闸:hard=0 且 uncertain=0;R-VERIFY 责任闭环(每个问题字段须 改设计|有证据|用户显式接受/书面处置)

## 任务形态分级(task_grade,代码判定)

paper_grade 触发(任一):source.kind=paper / experiment_reference.kind≠none /
comparability.targets 非空 / objective 含对照解释类关键词 → 四段必填、全层审阅;
exploratory:四段可空(缺=soft 提示);一旦填写对照内容即时升级。
