# MOD-01 公共契约与版本兼容

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

为所有生产者和消费者提供同一套记录、错误、身份与版本规则；不承担流程推进和科学判断。

- 建议责任岗位：契约维护开发。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/contracts`。
- 当前依据：redmaple-agent/src/shared.js；docs/development-spec/02-contracts.md。
- 需求主责：MIG-089。
- 通用需求主责：无独占 GSF；相关要求经依赖模块集成。
- 实施依赖：无其它模块实现前置；以现有基线与规范为输入。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：2–4 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `src/records.ts`：公共记录头与各类 DTO
- `schemas/*.schema.json`：JSON Schema 2020-12
- `src/validation.ts`：解析、验证、错误路径
- `src/compatibility.ts`：版本及旧字段映射
- `test/contracts/*.test.ts`：正反 fixture

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `validateRecord(type, value, schemaVersion) → ValidatedRecord / SCHEMA_INVALID`
- `canonicalDigest(value, algorithmVersion) → sha256 摘要`
- `migrateRecord(sourceType, sourceVersion, value) → {record, originalRef, warnings}`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

所有领域任务使用 workspaceId/caseId/revisionId/stageInstanceId/attemptId；纯检索允许无 case。camelCase 与新规范一致，旧 not_met/cannot_compute/script_only 只在边界映射。大小写、路径分隔符、null/缺省、非有限数值、未知字段必须明确拒绝或按扩展命名空间处理。摘要必须区分 bytes-v1 与 canonical-json-v1，不能覆盖红枫旧 fingerprint 含义。公共头 producer 由宿主填充，模型输入中的同名身份字段不能获得权威性。

## 5. 数据、存储与所有权

契约库不自建数据库。ErrorEnvelope、CommandEnvelope、ReceiptEnvelope、ArtifactRef、ResolvedLock 是跨模块稳定入口；业务记录的细节服从上层02契约，新增类型必须登记生产者/消费者。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 先建立 record 类型到 schema 的注册表与最小 fixture。
2. 使用稳定序列化、引用类型和版本适配器；旧值保留在 import metadata。
3. 实现生产者/消费者共享验证；未知主版本阻塞写入，历史只读可返回不兼容诊断。

异常与恢复：SCHEMA_INVALID 指明 JSON Pointer；VERSION_INCOMPATIBLE 不自动转换；REFERENCE_MISSING 不补默认空文件。无副作用，校验错误不重试。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-01-1 | 冻结记录注册表、错误表及兼容 fixture | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-01-2 | 实现 schema/摘要/迁移接口并接消费者 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-01-3 | 执行跨模块 round-trip 与破坏性变更合约检查 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-01-1 可先完成不依赖外部工具的契约/fixture准备，DEV-01-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-01-01 | 合法 ReviewReceipt round-trip，字段与摘要不变。 | notRun；无产品通过声明 |
| ACC-01-02 | 伪造 producer、unknown applicability 配 passed、null 替代必填字段被拒。 | notRun；无产品通过声明 |
| ACC-01-03 | 旧 snake_case 映射保留原值；未知枚举拒绝猜测。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

