# 02 数据与接口契约

## 1. 通用表示规则

记录采用 UTF-8 JSON 或等价 YAML；YAML 解析为 JSON 数据模型后校验，拒绝重复键、自定义执行标签、循环别名及非有限数值。对外规范字段采用 camelCase。未知核心字段必须报错；扩展只放 `extensions`，以反向域名或包 ID 命名空间隔离，不能改变核心语义。

- ID：非空稳定字符串；业务 ID 不根据文件修改时间生成。版本字段独立，不能复用 ID 暗示同内容。
- 时间：带时区的 RFC 3339，持久化用 UTC，界面按用户时区显示。
- 内容摘要：`sha256:<64位小写十六进制>`；文件按原始字节，目录闭包按已排序的路径/长度/摘要清单做明确规范化。序列化算法必须版本化。
- 路径：包内引用使用 POSIX 相对路径，不允许绝对路径或 `..`；解析符号链接后也必须位于允许根内。外部数据使用独立 locator，不假装包内文件。
- 可空性：未列为 nullable 的字段不得为 null；可选字段缺省表示“未提供”，不自动表示 false、零或不适用。
- 数量：数值与 unit 同时存在；无量纲用 `1`，枚举/分类用明确类型，不伪造单位。工具内部单位与报告单位要有转换记录。

## 2. 公共记录头

所有持久业务记录必须含 `schemaVersion:string`、`id:string`、`createdAt:timestamp`、`producer:{actorId,actorInstanceId,kind}`、`context:{workspaceId,caseId?,revisionId?,stageInstanceId?,attemptId?,operationId?,runId?,studyId?}`。`kind` 为 human/agent/service。领域任务记录必须有 caseId/revisionId；纯知识查询可不建案例。

生产者身份由宿主认证填入，不能信任模型自己声明。记录至少关联一份输入或明确标记是用户原始输入。对涉及科学执行的记录，必须带 `lockRef`；只读取历史资料时可标 `provenanceStatus: imported_unknown`，不得补造旧版本。

## 3. 记录最小字段

下表的“必需字段”为公共头之外必需内容，数组为空仅在语义允许时有效。实现 Schema 必须进一步约束互斥、枚举和条件字段，不能只验证对象存在。

| 记录 | 必需字段及类型 | 关键约束 |
|---|---|---|
| TaskSpec | intent:string；scope:enum；problem:object；constraints:object；requestedDeliverables:string[] | scope=new/modify/execute/analyze/visualize/query/diagnose/resume/reproduce；领域 problem 按独立 schema 校验 |
| ResolvedLock | packs:VersionRef[]；workflowRef；resourceRefs:VersionRef[]；adapterBindings:object[]；policyRef；digest | 每个最终引用精确版本/摘要，范围版本不得留在锁结果 |
| CaseState | revision:number；activeRevisionId；stageInstances:object[]；blockedReasons:object[]；counters:object[]；pendingOperations:string[] | revision 单调递增；状态变更走 CAS，不由导出文件反写 |
| ArtifactRef | artifactId；type；schemaVersion；locator；digest；byteLength:integer≥0；dependencies:ArtifactRef[]；status | status=available/missing/quarantined；大闭包可引用 manifest 防止无限嵌套 |
| ArtifactMetadata | artifactRef；producerStage；toolRef?；semantics:object；lockRef | 数值产物必须描述单位、关联对象、坐标、时间/事件基准及精度 |
| EvidenceRef | sourceId；sourceRevision?；digest?；locator；location:object；sourceKind；originGroupId；applicability:object；readReceiptRef | sourceRevision/digest 都无则标 unfrozen，并由证据政策决定可否用于该决策 |
| SourceReadReceipt | sourceRef；readScope:object；representation:enum；accessedAt；contentDigest?；limitations:string[] | representation=fullText/section/abstract/metadata；不能把摘要写成全文 |
| DecisionRecord | decisionKey；value；evidenceRefs:array；status；rationale；impactRefs:array | status=proposed/locked/reopened/rejected；必需证据未满足不得 locked |
| StagePacket | inputRefs:array；outputRefs:array；decisionRefs:array；selfChecks:array；diffRef?；risks:array | 生产完成不是审查通过 |
| ReviewReceipt | subjectDigest；checkSetRef；producerInstanceId；reviewerInstanceId；results:array；verdict；issueRefs:array | verdict=PASS/REVISE/BLOCKED；独立审查身份不同且无生产写权限 |
| CheckResult | checkId；applicability；result；evidenceRefs；observations；action | applicability=true/false/unknown；result=passed/failed/insufficient/notApplicable；unknown 不能 passed |
| RunSpec | inputClosureRef；adapterBinding；executionMode；resources:object；outputs:object[]；authorizationRef | executionMode=preflight/dryRun/real；授权可引用既有范围，不强制每次询问 |
| RunRecord | runSpecRef；operationId；status；submissionEvidence；inputDigest；outputRefs；logRefs；toolEnvironment；timestamps:object | 外部 ID、退出码按实际可用性提供；无退出码的远程 API 不伪造 0 |
| MetricDefinition | quantity；valueType；unit；methodRef；selection；reference；sampling；outputRequirements；acceptanceSpec | 方法/窗/方向等可标明确不适用但不可遗漏未说明 |
| MetricResult | definitionRef；dataRefs；value?；uncertainty?；verdict；limitations | verdict=met/notMet/cannotCompute/borderline；cannotCompute 必须说明缺口 |
| ValidationReport | dimension；methodRef；referenceRefs；dataRefs；outcome；limitations | dimension=numerical/model/task；outcome=passed/failed/insufficient/notApplicable |
| FigureRecord | dataRefs；analysisRefs；toolRef；selection；outputRefs；status；inspectionRef? | generated/scriptOnly/skipped/failed；generated 必须有真实图像核验 |
| ChangeProposal | targetRef；sourceRefs；diffRef；reason；validationPlan；status | draft/evaluated/accepted/rejected/applied；applied 须有发布回执 |

本规范使用 camelCase 枚举；旧 `not_met/cannot_compute/script_only` 导入时明确映射为 `notMet/cannotCompute/scriptOnly`，原值保留在导入元数据。未知旧值拒绝猜测。其它旧状态按逐字段映射表处理，不能做全局字符串替换。

## 4. 端口操作

| 端口 | 操作与输入 | 输出与副作用 |
|---|---|---|
| PackResolver | resolve(packRef, task, environment) | ResolvedLock 或结构化诊断；只读，不运行科学工具 |
| Kernel | dispatch(command, expectedStateRevision) | committedRevision、eventRefs、outboxRefs；唯一状态写入口 |
| ArtifactStore | publish(stagedContent, metadata)、resolve(ref)、verify(ref) | 不可变 ArtifactRef / 完整性结果 |
| RuntimePort | executeRole(roleTask, operationId)、reconcile、cancel | 会话回执、未知或取消结果；不签领域 PASS |
| ToolPort | preflight(RunSpec)、submit、status、collect、reconcile、cancel?、resume? | 作业/运行回执；可选能力必须显式声明 |
| KnowledgePort | search(query)、read(sourceRef)、validate(sourceRef)、propose(change) | 搜索页、读回执、有效性、候选；propose 不发布 |
| ReviewPort | submit(reviewReceipt) | 已核身份和契约的审查引用；是否推进由 Kernel 决定 |

以上是逻辑接口，不规定 HTTP/REST。实现以内部模块接口优先，只有真实跨进程需求时再定义网络协议。

## 5. 错误与重试

错误结构：`{code, category, message, retryable, sideEffectState, context, evidenceRefs, suggestedAction}`；`sideEffectState=none/confirmed/unknown`，不能仅凭 retryable 决定重试。

| code | 处理 |
|---|---|
| SCHEMA_INVALID / REFERENCE_MISSING / POLICY_CONFLICT | 修声明或补材料，不自动重试原请求 |
| CAPABILITY_UNAVAILABLE / VERSION_INCOMPATIBLE | 重新解析兼容绑定或阻塞，不偷偷换模型 |
| EVIDENCE_INSUFFICIENT / REVIEW_REQUIRED / ARTIFACT_CHANGED | 补证据/重审；旧 PASS 不复用 |
| STATE_CONFLICT | 重新读取状态并评估命令，不能无条件重放副作用 |
| SUBMISSION_UNKNOWN / RESULT_UNKNOWN | 对账，确认未发生后才可重新提交 |
| RESOURCE_LIMIT / DEADLINE_EXCEEDED / CANCEL_REQUESTED | 停止新操作；已提交外部工作继续跟踪到已知状态 |
| TOOL_FAILED / SCIENTIFIC_CHECK_FAILED | 保留证据、诊断与有界修复；不得自动调判据 |

只读查询的暂态网络失败默认最多重试 2 次，退避 1s、3s，受剩余截止时间约束；这些是工程默认，可由执行策略收紧或显式调整。有副作用提交无“通用自动重试”：相同 operationId 必须返回既有回执或进行对账。明确确认未执行才允许再发，且保留操作历史。

## 6. 事件、顺序和版本

事件必需：eventId、caseId、stateRevision、eventType、payloadSchemaVersion、occurredAt、causationId、operationId?、payload。每案例按 stateRevision 排序；可至少一次投递，消费者按 eventId 去重。外部晚到回执保留但不得覆盖已切换方案的当前状态。

同一 operationId 加不同请求摘要返回冲突；重复的相同回执不得再次推进或计数。跨案例可并行，同案例状态提交需串行化/CAS。取消是意图，只有确认工具停止或未启动才能记 cancelled；暂时无法联系记 unknown。

契约版本使用主/次版本；字段语义、必填项、默认值、状态变化属于兼容性分析对象。旧记录通过显式迁移器读取，迁移保留原文和导入时间；不能填入从未发生的审查、运行或哈希证据。
