# 06 入库与存储接口

## 1. 逻辑模型

所有实体采用稳定 ID 和不可覆盖的内容修订，引用使用 id＋revision。实体间关联表达领域含义，具体物理表示由后端适配器决定。

| 实体 | 最低业务字段 | 关系 |
|---|---|---|
| AuthoringSession | 目标、范围、用户职责、采用策略、当前活动、待答问题、准备度 | 关联领域包及材料批次 |
| IntakeBatch | 来源清单、贡献者、接收方式、使用范围、进度 | 分批关联来源修订 |
| SourceRecord | 标题、来源性质、作者/机构、版本、locator、摘要可用性、读取范围、使用/导出范围 | 关联片段及同源组 |
| SourceFragment | sourceRef、位置、摘录/转录、解析方式、覆盖/歧义 | 支持知识或条款 |
| TagDefinition | 标签面、名称、定义、别名、关系、状态、用户决定 | 关联标注 |
| Annotation | targetRef、sourceAnchor、tagAssignments、建议与采用状态 | 标注来源/知识/步骤 |
| KnowledgeUnit | 类型、表述、条件、范围、来源、反例、认识状态、采用用途 | 关联任务/案例/规范 |
| CaseRecord | 情境、基线、判断、动作、结果、原因假设、限制、迁移条件 | 各阶段关联片段/知识 |
| StandardClause | 发行方/版本、原文、条款、效力属性、适用性、例外 | 派生检查候选 |
| ReviewDecision | 主体修订集合、采用/修改/暂缓/拒绝、用途、理由、决定者 | 约束采用和发布 |
| AdoptionRule | 用户认可的确定性条件、元数据/标签赋值、范围与例外 | 覆盖相应后续自动整理动作 |
| DesignBinding | 知识修订、包目标位置、用途、转换理由、采用记录 | 模型/流程/检查/指标 |
| Gap / Question | 当前问题、影响、已有信息、待答/待取得项、恢复条件 | 引导下一动作 |
| ApplicationReceipt | 当前任务、取用知识修订、实际动作、效果证据和判定 | 形成经验修订候选 |
| CommitReceipt | 操作身份、请求摘要、期待修订、提交对象、内容/读取模型状态与问题 | 支撑写入对账与恢复 |

交换结构的机器约束见 [authoring-bundle.schema.json](schemas/authoring-bundle.schema.json)。schemaVersion 与各实体 revision 独立。

## 2. 公共字段

记录具有 id、revision、createdAt、actor、recordType、data。知识相关记录分别保存 lifecycle、epistemicStatus、provenance 和 useDecision。空缺数据使用明确 unknown/gap 或未提供，数字、单位和使用条件按领域扩展定义。

用户口述保存可回查的会话摘录/转录身份。AI 归纳记录 analystReconstruction 及其原始依据。基于某条规范作出的项目决定保留原条款属性和项目采用属性。

逻辑关系包含 fromRef、relation、toRef、scope、basisRefs。supports、challenges、derivedFrom、appliesTo、requires、supersedes 等关系分别定义。关系推理能力可以作为扩展，默认只按已记录关联读取。

## 3. 读写端口

以下为待实现逻辑端口，调用形式可按应用架构选择。

| 操作 | 输入 | 输出/语义 |
|---|---|---|
| capabilities | 空或作用域 | 记录修订、条件写入、批次可见性、查询、快照和内容读取能力 |
| registerSource | 原始身份、使用范围、幂等键 | sourceRef、duplicateCandidates、接收回执 |
| stageChange | 基础修订、候选记录/关系/删除意图、目标范围 | changeSetId、预览、差异、校验问题 |
| recordDecision | 用户决定、主体修订摘要、采用范围 | decisionRef，所用身份由宿主验证 |
| commitChange | changeSetId、decisionRefs、expectedRevisions、operationId | commitReceipt、提交修订、索引状态或冲突 |
| read | ref、读取范围、调用者 | 内容、来源、实际修订及读取回执 |
| query | 文本/标签/领域/任务/状态/条件/访问范围、分页 | 命中条目、来源定位、采用状态、匹配理由 |
| trace | ref、关系方向/类型/深度 | 上下游来源、决定、包绑定与影响范围 |
| exportSnapshot / importSnapshot | 范围、允许导出内容、修订集合 | 交换包、校验清单、导入预览和实际回执 |
| validateReferences / rebuildReadModel | 目标快照/变更点 | 引用/读取模型有效性与缺口 |

向量检索、全文索引、关系遍历加速和多机部署可声明为可选能力。系统可使用可用的检索方式返回诚实的覆盖信息。

## 4. 入库与生效

资料登记和候选保存立即形成可恢复记录。将语义标签和知识投入用户指定用途时，验证采用决定覆盖的主体修订、范围和权限。已批准的提取/标注规则可以覆盖同范围的后续动作，并保留规则引用。

一次变更的有效对象集包括记录、关系和相应决定引用。后端若支持原子批次，按其能力完成条件提交。若采用应用层操作日志，则在发布可见修订前完成校验和恢复对账，读侧按已提交变更集合取数。

决定针对明确的候选修订及其 contentDigest，canonicalization 字段为 RFC8785。采用 JSON Canonicalization Scheme 生成 UTF-8 字节后计算 SHA-256，跨语言实现通过同一兼容夹具验证。

摘要的逻辑对象为 `{recordType,data}`。knowledge 的 data 排除 lifecycle、useDecision、decisionRefs，tag/annotation/binding/rule 排除 status、decisionRefs，clause 排除 projectApplicability、decisionRefs，其余语义字段全部参与。会话范围采用摘要仅含 objective、targetPackId、scope。决定同时声明 intendedUse 和 scope，提交时独立校验其与生效用途的匹配。采用后产生的新内容修订只允许更新上述状态字段并保留决定，语义变化需新的决定或已认可规则覆盖。

采用修订保存 adoptionBaseRef，指向同一 ID 的已审候选修订，候选和采用后的修订共同保留。校验候选摘要、采用内容摘要、决定主体及用途/范围全部匹配后生效。用户直接编辑并采用的内容可在一次逻辑提交中保存候选、决定及采用修订。

AdoptionRule 的条件使用已声明事实和 eq/in/exists 运算，动作覆盖 metadataAssign/tagAssign。规则保存已接受的标签引用，元数据 fieldsMap 限定为调用方允许更新的登记字段。系统执行时验证规则状态、范围、例外与用户决定。知识主张、模型/验收阈值和行业义务的变化进入对应语义采用流程。

部分写入故障标为 staged/reconciling，重试使用同一 operationId 和请求摘要。相同操作请求重复返回原回执，不重复创建记录。相同 ID 异请求返回冲突。期待修订不匹配时返回双方变化，保留用户后续编辑。

临时演示记录带 originMode=demonstration，实际制作会话引用其内容时先形成候选和明确的演示来源。实际用户采用由宿主身份和决定回执记录。

源内容提交与检索模型更新可以分步，commitReceipt 应报告 contentCommitted 与 readModelState。检索落后时显示已知版本，并提供按 ID/来源读取，状态和采用范围仍在应用服务中校验。

## 5. 建议错误类别

INVALID_RECORD、REFERENCE_MISSING、SOURCE_CHANGED、DECISION_SCOPE_MISMATCH、REVISION_CONFLICT、TAG_AMBIGUOUS、UNSUPPORTED_CAPABILITY、ACCESS_SCOPE_MISMATCH、PARTIAL_COMMIT、RESULT_UNKNOWN、READ_MODEL_STALE。

错误包含对象、位置、当前修订、可恢复动作和相关证据。审阅不足只限制相应采用动作，候选与原始资料仍可保存。

## 6. 查询视图

制作工作台允许查询候选、争议、待答与已采用知识。执行上下文按领域/任务/条件/访问范围筛选，优先返回被采用为相应用途且修订有效的条目。每项结果说明 sourceRefs、knowledgeRef、annotations、decisionRefs、scopeMatch 和局限。

来源撤回、标签修订、知识替代或决定收窄后，建立影响记录。查询与新包构建使用新的有效状态，既有包锁保留历史引用并标待复核。可追踪范围以实际已建立关联为准。

## 7. 导出、迁移和选型

交换包保存 session、实体、关系、决定、标签词汇修订、来源定位和内容摘要，原始字节按使用范围选择嵌入或外部引用。导入先验证摘要/引用/ID冲突和语义版本，再由用户选择新增或映射，保留原身份与导入回执。

不同后端需通过同一能力契约与故障场景。业务语义、标签、用户采用范围和知识来源应能随交换包迁移。数据库选型在实际数据量、查询模式、并发、部署和维护条件明确后再评估。
