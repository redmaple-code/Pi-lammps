# 04 工作流与状态机

## 1. 统一责任链

入口分类→解析领域包→证据与问题定义→方案和验收锁定→领域构建阶段→就绪检查→执行→数值/模型验证与分析→交付→反馈。

这些是责任，不是固定阶段数量。查询无需创建案例；分析/绘图等部分入口必须校验其实际输入与可支持的结论。只执行不自动承诺科学验收，复现要区分完整、带假设、部分和阻塞。

## 2. 图校验与调度

- 同级依赖图必须无环；阶段 ID 唯一；引用存在；输出插槽有唯一生产者或显式选择器。
- 并行仅适用于无写冲突、依赖满足且预算允许的分支；共享资源由执行层分配。
- 汇合默认等待所有适用前驱满足。跳过节点的输出不能被视为存在；下游需有明确替代输入或同样不适用。
- 输入、判断事实、检查集及其版本在阶段开始前绑定；执行中变化触发冲突或新 attempt，不静默读取新文件。
- 循环只通过显式 iteration 节点；每轮有稳定 iterationIndex 和独立子阶段身份，记录停止原因与预算。

## 3. 阶段状态

| 当前→目标 | 触发与必要条件 |
|---|---|
| pending→ready | 必需依赖通过、输入可解析、when=true、预算/授权满足 |
| pending→skipped | when=false 且有事实证据；不产生虚构输出 |
| pending/ready→blocked | unknown 条件、资料/能力/决策缺口；保存 reasonCode |
| ready→running | 执行意图和 operationId 已持久化 |
| running→awaitingReview | 生产产物提交完成，当前阶段要求审查 |
| running→passed | 该阶段无需独立审查且所有必需程序检查通过，有真实完成回执 |
| awaitingReview→passed | 独立审查 PASS，输入摘要和检查版本仍匹配 |
| awaitingReview→needsRevision | REVISE，计数更新，明确问题和重审条件 |
| awaitingReview→blocked | BLOCKED 或触及修订上限 |
| needsRevision→ready | 修订任务明确，计数/预算允许；新 attempt，保留旧产物 |
| running→failed | 已确认动作失败，不等于后续无法修复 |
| 任意非终态→cancelRequested | 用户取消或策略触发；先阻止新增操作 |
| cancelRequested→cancelled | 所有受管外部操作已确认停止或未提交 |
| running/cancelRequested→unknown | 外部结果不可确认；需对账，不能当失败后重跑 |
| blocked/failed/unknown→ready或对应已知状态 | 经补件/修复/对账命令及证据恢复，保留原因和前史 |

passed/skipped/cancelled 是该次阶段实例的终态；失效通过新增 invalidation 事件和方案/attempt 表达，不直接覆盖旧终态。上述 unknown、cancelRequested 是对通用设计概念状态的工程细化。

## 4. 审查和独立性

审查对象是固定 input/output 闭包，不是文件夹最新内容。ReviewReceipt 必须指向 producerInstance、reviewerInstance、规则版本、逐项证据和裁决。宿主验证身份和结构；科学内容由适用检查器或有独立职责的审查者判断。

原角色 reasoner 的咨询不能签 reviewer 的放行。审查失败不可由 coordinator 改写为 PASS；需修订或显式处理缺口。统一最终展示应保留阶段审查、运行状态和科学验收的不同结果。

## 5. 触发与裁决

when/check applicability 使用 true/false/unknown 三值。all 中任一 false 为 false，否则有 unknown 为 unknown；any 中任一 true 为 true，否则有 unknown 为 unknown；not unknown 仍 unknown。必须记录决定所用事实和版本。

检查触发判断与检查结果分开：如“使用 pair_coeff”是触发，“映射正确”是结果。未知且涉及必需规则则 blocked；false 需要可定位的不适用依据。

规则 onFail 为 revise 或 blocked；内核按最强适用动作汇总。LAMMPS MB-001–007 违规保持 blocked，不能套通用默认 revise。任何必需检查 insufficient 都不能 PASS。

## 6. 计数、修复和迭代

两套计数分别维护：同阶段连续审查 REVISE；同修复链连续失败。计数绑定 case、revision 和稳定阶段/修复链身份；重启会话、改问题名称或新 run 不重置。阶段通过可结束该连续序列，但历史累计不删除。合法科学重设计生成新 revision，显式关联旧修复链，并服从案例总预算。

LAMMPS 两套上限默认均为 3。其它包可在宿主允许范围内配置；上限到达时停止自动修复并给出诊断、已试修改、剩余缺口。延长预算必须是记录清楚的策略变更，不能通过删计数实现。

循环达到最大次数、预算或截止时间时，保存 bestAvailable 和停止原因，验收仍按原标准判断；不因“循环结束”变为收敛。

## 7. 外部提交与崩溃恢复

1. 内核事务保存 intent、请求摘要与 operationId。
2. outbox 消费者提交；相同 operationId 只关联同一请求。
3. 工具返回 jobId/回执，记录后收集状态与产物。
4. 若提交后回执前崩溃，以 operationId/调度标签/进程身份对账。
5. 找到已提交工作则接管查询；确认未提交才重新发出；无法确认保持 unknown。
6. 完成回执经过输入摘要、产物和状态检查后推进。晚到旧方案回执只记录，不推进当前方案。

不要求所有工具实现 exactly-once；要求遇到无法保证时诚实进入 unknown 并避免重复副作用。

## 8. 失效与恢复范围

变更→按工件依赖闭包计算受影响对象→标记不可用于当前结论→定位最早需要重做的阶段→重审/重算受影响部分。对确实不受影响的复用记录保留依据。

图样式修改通常只重画；分析窗/单位/公式修改使分析与图失效；输出不足可能需补算；模型、几何、边界、材料或验收变化需回到对应方案阶段。环境恢复不应默认重建科学模型。

恢复以权威状态、锁、工件摘要、操作对账和数据来源为依据。文件 mtime、最后一句对话或“latest”目录不构成可信恢复点。
