# MOD-02 Red Maple 运行时适配与角色任务

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

将角色任务变为独立 Pi 会话和可核回执，保留预算、取消、上下文与调用追踪；不将自然语言结果直接写成阶段通过。

- 建议责任岗位：Agent 运行时开发。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/runtime-redmaple`。
- 当前依据：redmaple-agent/src/full-runtime.js；src/native-workflow.js；src/platform-extension.js；src/durable.js。
- 需求主责：MIG-005、MIG-006、MIG-007、MIG-009、MIG-010、MIG-014、MIG-017、MIG-024。
- 通用需求主责：无独占 GSF；相关要求经依赖模块集成。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-18](MOD-18.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：3–5 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `src/runtime-port.ts`：公开 SDK 装配
- `src/role-registry.ts`：原角色别名/模型/能力/轮次
- `src/context-builder.ts`：短交接与来源引用
- `src/simulation-task.ts`：durable桥接
- `test/runtime/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `executeRole(roleTask, operationId, signal) → RoleReceipt`
- `reconcile(operationId) → succeeded/failed/unknown 回执`
- `cancel(operationId) → CancelReceipt`
- `buildContext(roleId, lockRef, inputRefs, budget) → ContextPack`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

13 个原定义保留别名和来源；hello 不计领域职责。paper reproduction auditor 是 skill 任务映射，不能伪造旧独立角色。writer/reviewer 分 session 和 capability；reviewer 只通过专用 submit 工具发布回执。coordinator 只收状态摘要与短交接；技术 JSON 原件留给验证器和审查角色。原 maxTurns 与 Pi 模型调用分别计量。模型、角色 prompt、技能、上下文包摘要固定在任务开始时。恢复使用精确 operationId，不用开始时间推断属于哪次操作。

## 5. 数据、存储与所有权

宿主拥有会话与调用账本；SimulationStore 只存 session/operation/call 引用和短回执。上下文预算纳入已锁定规则和必需证据，不因裁剪静默删除强制项。源的 RESULT/LOG/NEXT 仅适用角色保留。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 验证 roleTask、角色版本、输入闭包与模型能力，并建立 operation 意图。
2. 通过 createRedMapleRuntime/公开导出创建子会话，在同父预算内执行；角色能力从宿主传入。
3. 收集确切调用/工件、校验结构化返回；未知调用进入对账，最终关闭会话并保留证据。

异常与恢复：role/model 不存在返回 CAPABILITY_UNAVAILABLE；预算耗尽停止新调用；已发送无回执为 RESULT_UNKNOWN。不得重新开会话清掉 unknown。超上下文返回明确缺口，不删除门禁要求。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-02-1 | 抽取角色配置和SDK适配边界，建立受控假模型 fixture | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-02-2 | 实现 RoleTask/上下文/持久任务桥接 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-02-3 | 核验角色隔离、预算、取消、恢复与全部旧路由 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-02-1 可先完成不依赖外部工具的契约/fixture准备，DEV-02-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-02-01 | 两个并发角色同预算但 operation 不同，调用不会串归属。 | notRun；无产品通过声明 |
| ACC-02-02 | writer 经 Code Mode/MCP 尝试 reviewer.submit 被拒，reviewer不能写生产工件。 | notRun；无产品通过声明 |
| ACC-02-03 | 在子会话结束前终止宿主，恢复找到同操作，不盲目再发。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

