# MOD-20 研究活动、有界迭代与耦合交换

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

支持扫描、收敛、随机重复、校准/优化以及显式跨尺度交换；作为K1/K3/K7组合，不能另建旁路状态机。

- 建议责任岗位：科学工作流开发；耦合模型审查人员。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/kernel/src/studies 与 adapters/exchange`。
- 当前依据：docs/跨领域通用仿真工作流与领域包设计.md §7.3–7.4/9/11；通用开发规范。
- 需求主责：无独占 MIG；承担下游集成/交互，相关要求通过共享契约关联。
- 通用需求主责：GSF-004、GSF-016。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-03](MOD-03.md)、[MOD-05](MOD-05.md)、[MOD-09](MOD-09.md)、[MOD-11](MOD-11.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：4–7 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `study-plan.ts`：样本/种子/受控变量
- `iterations.ts`：有界展开/停止
- `exchange-contract.ts`：量/单位/时空映射
- `checkpoint-bundle.ts`：一致恢复点
- `test/studies/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `expandStudy(studySpec, budget) → RunIntent[]`
- `evaluateStop(iterationRefs, criterionRef) → converged/limitReached/noProgress/cancelled/insufficient`
- `validateExchange(exchangeRef, contractRef) → ValidationReport`
- `restoreCoupledCheckpoint(bundleRef) → RestorePlan`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

研究活动独立studyId，run关联样本/随机种子/iteration而非文件名。DAG内部循环只能显式有界展开，预算/最大次数/无进展停止与收敛分开。校准集和最终验证集隔离。交换量需quantity/unit/coordinateFrame/entityAssociation/timeReference/mapMethod/errorBounds；双向耦合定义同步、松弛和收敛准则。恢复使用共同时间点与交换日志束，不能各子求解器挑最新文件。单体联合求解只是ToolPort一种绑定，仍走同门禁。

## 5. 数据、存储与所有权

StudySpec/IterationReceipt/ExchangeRecord/CheckpointBundle为新增契约扩展，必须在MOD01注册。共享K6事务和operationId；父预算分配给子run并保留实际消耗，不重复扣费或绕上限。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 展开参数/种子计划并锁定受控条件、预算和停止判据。
2. 逐iteration发run，核exchange语义与误差后更新下一轮；缺材料暂停。
3. 保存同步checkpoint bundle，按明确收敛/上限/取消结果汇总验证。

异常与恢复：单位/坐标/实体映射不兼容阻塞交换；maxIterations到达不标converged；单子求解器unknown使受依赖耦合分支等待对账。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-20-1 | 冻结Study/Exchange/Checkpoint契约及最小反例 | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-20-2 | 实现有界活动展开、交换核验与同步恢复 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-20-3 | 用两类研究活动和最小耦合fixture验证非旁路语义 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-20-1 可先完成不依赖外部工具的契约/fixture准备，DEV-20-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-20-01 | 随机重复相同seed可追溯，改变样本仍受父预算限制。 | notRun；无产品通过声明 |
| ACC-20-02 | 残差未达标但迭代用尽，结论limitReached且不宣称数值验证通过。 | notRun；无产品通过声明 |
| ACC-20-03 | 两个子求解器checkpoint时间不同，恢复拒绝并要求共同一致束。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

