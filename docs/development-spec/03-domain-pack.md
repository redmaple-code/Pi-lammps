# 03 领域包规范

## 1. 包内容与结构表示

领域包必须包含身份/范围、问题定义、知识、模型、流程、工具需求、检查、计量、失败诊断、模板、角色上下文及验证案例的声明。文件组织沿用[通用设计](../跨领域通用仿真工作流与领域包设计.md)第 4 节；内核只消费解析后的规范表示，不依赖物理文件夹名称。

[domain-pack.schema.json](schemas/domain-pack.schema.json)定义 v1 规范表示：文件布局可使用 manifest.yaml 和外部 YAML；包加载器解析后必须形成该表示，外部资源仍由资源表引用。该 Schema 同样可直接用于维护小型单文件 JSON 包。

| 字段 | 内容 |
|---|---|
| schemaVersion / id / version | 结构版本、包身份和科学内容版本；三者不可混用 |
| publicationStatus | draft/released；draft 不得进入生产真实运行 |
| releaseReceiptRef | released 必填，指向 validation 资源中的实际发布回执 |
| scope | 领域、尺度、支持任务和明确排除项 |
| kernelCompatibility | 内核契约兼容范围 |
| dependencies | 其它包的版本范围；解析锁中必须固定版本与摘要 |
| resources | 具名资源的类型、包内路径、可用性与摘要 |
| entrypoints | 各类资源入口，指向 resources 中的 ID |
| requiredCapabilities | 当前包必要能力；任务特定能力可由工作流再收紧 |
| toolProfiles / defaultToolProfile | 能力到适配器/软件版本的绑定、验证状态及默认配置；validated 绑定必须有 validationReceiptRef |
| workflows / defaultWorkflow | 具名工作流及默认值；每个工作流声明外部输入和阶段图 |
| policies | 证据、独立审查和两种修订上限；不能放宽宿主策略 |

## 2. 资源与引用

resource 的 `kind` 为 problemSchema/artifactSchema/knowledge/model/vocabulary/check/metric/failure/template/role/validation/protocol；`availability` 为 available/missing。available 必须提供真实 digest 并可读取校验；missing 必须报告缺口。样例将科学资产标 missing，避免假造已验证案例。

入口必须引用正确种类的资源。`resourceId#itemId` 表示资源内具名项，单独 resourceId 表示整个资源；片段必须在实际内容中存在。依赖包资源使用 `packId::resourceId#itemId`，必须位于已解析依赖集合。不得用全文搜索近似匹配代替身份解析。

资源路径需唯一；若多个入口共享同一文件，引用同一 resourceId。远程来源写在知识目录内容里，不在资源表里混用 URL 与包内路径。引用的外部资料不可取得时保留限制，不伪造内容摘要。

## 3. 解析顺序与结果

1. 安全解析 JSON/YAML，结构校验；未知字段和重复键报错。
2. 校验包 ID、资源 ID、工作流和阶段 ID 唯一，引用/默认值存在。
3. 解析依赖版本，检查循环依赖及内核兼容。
4. 读取必需资源，验证字节摘要、片段和具体领域模式。
5. 校验阶段图、输入输出、条件类型、能力、检查覆盖和迭代边界。
6. 合并允许的任务参数和项目策略，解决冲突；生成明确的有效配置。
7. 检查适配器、工具版本、环境、资源、许可证引用和现有授权范围。
8. 生成锁定计划及缺口。只有全部当前执行必需项满足，才能称 ready。

结构合法不等于资源齐全；资源齐全不等于科学适用；解析成功也不等于已运行。解析器应返回 `structurallyValid / semanticallyValid / executionReady` 分项及诊断，而非单一布尔值。

## 4. 流程声明

规范表示中工作流为 `id, taskType, inputs[], outputs[], stages[]`。阶段含 id、kind、role、needs、consumes、produces、checkRefs、reviewRequired、when?；role 引用角色资源中的职责 ID。action 另有 capability；iteration 另有 bodyWorkflowRef、iterationProtocolRef、maxIterations、untilCheckRef。

`needs` 指当前图内阶段 ID；consumes/produces 指类型化工件插槽名，每个插槽具体类型由 entrypoints.artifactSchemas 引用的资源声明。该资源至少定义 workflowId、slotName、artifactType、schemaRef、cardinality、semanticConstraints，基数为 one/many；类型和科学单位在跨阶段传递前核对。输入必须来自工作流外部 inputs 或可达的上游 producer。只有需要的数据和已通过依赖可消费，不按路径存在推断。

iteration 子流程不得直接或间接递归引用自身；子流程 inputs/outputs 必须与迭代节点插槽一致。iterationProtocolRef 必须指向 protocol 资源，声明 mode（refinement/replication/optimization/coupling）、initialBindings、carryBindings、aggregation、seedStrategy（如适用）和 budgetRef。initialBindings 指外部输入到首轮插槽；carryBindings 指上一轮输出到下一轮输入，重复试验可为空并保持模型输入不变；aggregation 按插槽指定 last/collect/registeredReducer，其中 reducer 必须绑定可信能力版本。未知策略不得默认处理。独立重复试验需记录每轮派生种子，并将样本累积后检查统计精度；最大次数仅是停止上限，不是通过条件。

公开任务的 taskType 必须在 scope.tasks 内；仅供 iteration 引用的内部子流程可使用内部 taskType，但不能直接对用户作为受支持任务入口暴露。

条件 DSL 只允许 `fact + op + value` 或 all/any/not 组合。op 为 eq/ne/in/gt/gte/lt/lte；事实路径来自已声明上下文。缺字段/类型不符产生 unknown；禁止 eval、shell、网络访问或任意函数调用。比较运算的类型和量纲需匹配。

## 5. 工具配置与环境分离

toolProfile 绑定 capability、adapterId、adapterVersionRange、toolVersionRange、validationStatus。机器路径、账号、凭证和调度队列放环境配置，敏感值只用秘密存储引用。已验证的绑定必须对应实际兼容性回执；不能手工把 unverified 改为 validated 就通过发布。

给任务换 profile 时核对模型支持、边界/初值、格式、单位、输出、许可证与验证案例。适配器接口一致只保证调用契约，不保证科学结果等价。

## 6. 修改、覆盖与版本

默认优先级：固定内核不变量→宿主权限/资源策略→领域包必需规则→项目中兼容的附加约束→任务允许覆盖的参数。权限取已授予范围；预算取有效上限；科学规则冲突返回诊断，不靠“更严格的字符串”猜含义。

包必须在 problemSchema 中区分可调参数、锁定模型选择和不可覆盖约束。配置修改生成新包内容摘要；影响行为或科学内容时发布新内容版本，不能在同版本地址覆盖内容。文档拼写修订也要保留新内容摘要，可仅提高补丁版。

版本兼容不只是 SemVer：同主版本也可能改变数值结果，旧方案仍使用原锁。模型、参数、阈值或工作流变化需单独说明科学影响。

## 7. 切换与发布

新任务可直接换包；已有任务换工具或包要创建新方案/案例，保存差异与迁移映射，并重评受影响依赖。跨尺度参数转换通过正式变换工件和验证，不能作为包切换的隐藏副作用。

draft 可编辑、解析和运行无副作用的结构检查；真实基准验证可在明确的验证沙盒/资源授权下针对 draft 开展，并标为 candidateValidation。生产入口不得选用 draft。发布需真实资源闭包、锁定依赖、兼容工具回执、正反验证案例和审阅记录。

发布过程是不可变归档＋发布回执；更新活跃选择只是指针变更，不改运行中的锁。为避免摘要自引用，发布回执绑定科学内容闭包摘要，该闭包明确排除发布状态、releaseReceiptRef 和它指向的回执资源；最终归档另计算包含发布元数据的 archiveDigest。两种摘要的范围与规范化版本必须记录，不能互相替代。撤回版本阻止新任务默认采用，标记关联旧案例需复核；旧材料保留供历史追踪。

## 8. 样例的使用限制

三份 examples 均通过结构校验并具备不同阶段结构；未提供科学模型、手册、检查器或真实验证数据。语义检查应指出 missing 资源和 unverified 工具配置，并拒绝生产执行。它们是领域包编写起点，不是可运行仿真项目，也不证明跨领域功能已经实现。
