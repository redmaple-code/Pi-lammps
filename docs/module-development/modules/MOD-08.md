# MOD-08 K6 权威状态、事务与恢复

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

提供唯一科学状态写入口、原子事件/outbox、幂等与恢复；与Pi对话和宿主调用账本保持职责分离。

- 建议责任岗位：状态与可靠性开发。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/kernel/src/state`。
- 当前依据：redmaple-agent/src/platform-store.js、src/sqlite-store.js、src/durable.js。
- 需求主责：MIG-063、MIG-066。
- 通用需求主责：无独占 GSF；相关要求经依赖模块集成。
- 实施依赖：[MOD-01](MOD-01.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：4–7 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `simulation-store.ts`：事务/CAS
- `migrations/*.sql`：带checksum迁移
- `outbox.ts`：领取/提交/对账
- `recovery.ts`：可信节点恢复
- `test/state/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `dispatch(command, expectedStateRevision) → CommitReceipt`
- `loadCase(caseId) → CaseState`
- `claimOutbox(workerId, lease) → OperationIntent[]`
- `recordReceipt(operationId, receipt) → CommitReceipt`
- `backup(target) / restore(snapshotRef)`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

一事务内检查前态/身份/摘要，写业务状态、事件、outbox与命令去重记录。事件每case按revision有序，允许至少一次通知，消费者按eventId去重。outbox lease到期只允许对账未知发送，不自动重发。晚到回执按原revision归档，不覆盖新方案。SQLite/WAL与锁放本地状态目录；同步盘只存一致性备份和不可变工件。单机owner恢复需证明原进程停止，跨机不能靠PID猜死。

## 5. 数据、存储与所有权

建议表：cases、revisions、stage_instances、counters、commands、events、operations、outbox、receipts、artifact_links、decisions、schema_migrations。唯一键workspace+case+commandId，operationId+requestDigest；变更需expectedStateRevision。业务JSON投影不可反向写状态。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 先校验命令与权限，再BEGIN IMMEDIATE读取最新状态做CAS。
2. 原子落状态/事件/意图，发布侧领取outbox并调用外部端口。
3. 恢复先校验锁/工件/运行绑定，再对账pending/unknown，最后补做失效或缺失部分。

异常与恢复：STATE_CONFLICT返回当前revision，由调用方重新评估；I/O失败回滚事务；DB版本过新只读诊断或拒绝。恢复不按mtime选最新run。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-08-1 | 实现实体/唯一键/事务迁移与命令去重 | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-08-2 | 实现outbox及owner/unknown恢复 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-08-3 | 做强杀、并发、备份恢复与版本迁移验证 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-08-1 可先完成不依赖外部工具的契约/fixture准备，DEV-08-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-08-01 | 两个写者相同revision仅一个成功，计数不丢失。 | notRun；无产品通过声明 |
| ACC-08-02 | commit后投递前、发送后回执前强杀，恢复不重复科学提交。 | notRun；无产品通过声明 |
| ACC-08-03 | 旧run晚到成功保留历史，不让新revision跳阶段。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

