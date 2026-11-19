# 04 标签、标注与知识表达

## 1. 标注对象

标注可以指向整份来源、特定片段、案例阶段、知识单元、条款、参数、流程步骤或验证结果。annotation 包含 targetRef、目标修订、精确位置/摘录、标签赋值、提出者、采用依据与状态。

片段定位支持 page/paragraph/line/cell/table/time/frame 等通用位置，具体坐标结构由来源类型扩展。文本摘录同时保留 quote 与周边范围摘要，来源改变后检查能否重定位。失配标为 stale，原标注继续保留。

## 2. 通用标签面与领域内容

| 标签面 | 标注内容 | 形成方式 |
|---|---|---|
| contentKind | 案例、经验、规范条款、方法、参数、失败、评价等 | 采用通用起始词汇，允许扩展 |
| domain/task/stage | 关联哪个行业、任务和流程位置 | 从用户目标、案例和包结构提议 |
| system/model/condition | 研究对象、模型、尺度、工况、工具/版本 | 由领域材料和用户语境归纳 |
| decisionRole | 前提、解释、边界、操作、评价、背景、争议 | 按具体设计决定绑定 |
| outcome/failureMode | 成功/失败/负结果、失败特征与待核原因 | 依据观察，原因另外标支持程度 |
| standardScope | 条款适用对象、行业/地域、版本与义务类别 | 来源提取＋项目适用性审阅 |

每个标签使用 facetId、tagId、displayName、aliases、definition、include/excludeExamples、status 和修订。标签 ID 稳定，改名保留别名。标签可以形成 broader/narrower/related 关系，关系类型与意义要明确。

来源版本、知识生命周期、认识判断、访问范围和采用状态以专门字段表达。标签可以投影这些字段用于筛选，底层字段承担权威值。

## 3. 提议、确认与维护

AI 对语义标签给出候选、理由和对应摘录，用户可采用、修改或拒绝。用户可以预先认可确定性标注规则，例如“来源类型等于设备日志时添加操作记录标签”。规则记录条件、目标、例外和生效范围，自动执行仍保留 assignmentBasis。

新行业分类先进入提案。用户提供的分类可直接作为用户组织视图保存，标注 userDeclared。若另做语料归纳，对照实际材料并记录异同。合并、拆分和废弃标签保留映射、修订和影响清单，歧义词按语境分别建项。

原文含有一个术语可以支持“出现该术语”的标注，知识是否适用于当前任务需进一步核对条件。标签置信度仅表示提取/匹配的估计或人工信心，不能替代证据判断。

## 4. 知识单元

知识按可解释、核查、使用或反驳的最小单元组织，采用 concept、claim、method、practice、observation、resource 六类起始类型。行业可以扩展类型，并保留到通用类型的映射。

每个单元具有稳定 ID、内容修订、表述、类型、适用范围、条件、来源/片段、例外、相关任务与证据状态。claim 可进一步注明 mechanism、limitation、empirical、normative、hypothesis 等性质。经验卡通常拆为 observation、practice 和一个或多个待核 claim。

研究方向用于导航，同一知识可以服务多个方向/任务。任务绑定保存 role、necessity、reason、scopeMatch；例如同一阻尼知识在某任务为必要操作依据，在另一任务只作背景。

## 5. 状态分别保存

| 维度 | 代表值 | 含义 |
|---|---|---|
| lifecycle | candidate/pendingReview/confirmed/quarantined/deprecated/retracted | 条目维护和采用进程 |
| epistemicStatus | unassessed/supportedInScope/contested/refutedInScope | 给定条件下的证据判断 |
| provenance | originalReport/laterReport/userAccount/analystReconstruction | 内容来自哪种叙述或归纳 |
| annotation.status | suggested/accepted/rejected/stale | 某个标签赋值的采用状态 |
| useDecision | referenceOnly/projectPractice/packCandidate/activeInScope | 允许怎样使用该条知识 |
| validationStatus | planned/notRun/passed/failed/inconclusive | 对应验证活动的实际结果 |

confirmed 由审阅和采用记录支撑，epistemicStatus 仍可为 unassessed 或 contested。用户可选择将带争议的条目作为提醒或对照资料，检索返回必须显示该状态和用途。

## 6. 检索与回查

工作知识查询默认采用已接受的语义标签与当前有效用途，并按领域/任务/条件/访问范围过滤。制作工作台可检索候选和争议。每条结果包含知识内容、适用条件、采用状态、来源定位、标签依据、修订与命中理由。

原始资料、片段、知识单元和包绑定互相可回查。标签变化更新筛选视图和命中解释，知识结论的改变仍需独立的内容修订与审阅。

## 7. 示例

有限元案例可以标记 task=implicit-transient、stage=configure、topic=damping、outcome=unexpected-response。对应经验可写为“在指定模型/频带条件下，检查阻尼拟合与输出采样”，来源为案例片段。具体原因、参数和有效性由用户材料与验证补齐。

小分子对接案例可以标记 stage=ligand-preparation、topic=protonation、decisionRole=operational。遇到排名变化，先保存实际观察，再追问准备条件、评分协议和对照。机制解释进入独立候选。
