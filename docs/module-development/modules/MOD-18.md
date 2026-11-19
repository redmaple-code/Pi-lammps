# MOD-18 Red Maple 与 Pi 基座优化

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

为仿真提供通用能力改进，同时保留独立Pi演进、旧SDK和通用任务的兼容性。

- 建议责任岗位：基座维护开发。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`redmaple-agent/src（现有）；Pi fork（条件性后续）`。
- 当前依据：redmaple-agent/src/session-controls.js、native-workflow.js、platform-extension.js、evolution.js、upstream.js、native-compaction.js。
- 需求主责：MIG-090、MIG-091。
- 通用需求主责：GSF-021。
- 实施依赖：无其它模块实现前置；以现有基线与规范为输入。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：3–5 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `src/role-operations.js`：拟新增通用角色操作接口，接入前核对package.json exports并提供消费者测试
- `src/session-controls.js`、`src/permissions.js`：现有文件的统一能力检查改动点
- `src/native-workflow.js`、`src/platform-extension.js`：现有两条委派路径复用操作接口、精确关联
- `src/evolution.js`：现有来源和评估协议的兼容扩展
- `src/upstream.js`、`src/native-compaction.js`：现有Pi内部适配集中点，不把私有符号暴露给业务
- `test/role-operations.test.js`、`test/runtime-compatibility.test.js`：拟新增操作/公开契约测试

本节路径相对redmaple-agent仓库根；已存在文件只是拟改动点，本轮未改。独立Pi fork目录与源码commit须在确有必要时定位，当前不编造路径或补丁。

## 3. 对外接口

- `拟新增公开RoleOperation能力：不暴露私有session成员给领域包`
- `统一beforeTool authorization与共享budget/reconcile`
- `能力清单{version, capabilities}，MOD02在启动时协商`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

先公开SDK/extension，再集中内部adapter，确有必要才fork Pi；基座不导入simulation或LAMMPS。当前Pi包1.5.0，源码commit尚需追溯，fork前锁确切发布基线。修复budget/unknown测试差异先区分发送前取消和发送后未知，不能清掉未知副作用换测试通过。旧UTF8 fingerprint、现有SDK参数与会话格式保留，新增bytes摘要单独version。memoryContextTokens当前按bytes计量需兼容解释与迁移。通用activationPolicy可保留，但仿真保护策略来自宿主profile。

## 5. 数据、存储与所有权

基座账本只记录实际调用/预算/取消/未知；科学状态保留在K6。新增公共导出需合同测试，私有Pi压缩接口变更集中在upstream/native-compaction。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 在25dec99复现受影响行为和旧SDK测试，列出最小公共接口变化。
2. 分离通用补丁与领域代码，锁定依赖构建；必要时维护小型上游patch集。
3. 稳定/实验runtime并存，按相同科学fixture和旧功能测试决定切换。

异常与恢复：能力不兼容返回VERSION_INCOMPATIBLE，不能静默丢能力；新API故障保留稳定版入口。真实模型性能收益需测，不靠单元测试宣称。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-18-1 | 复核基座失败与公开API缺口 | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-18-2 | 实现小范围通用接口/隔离/兼容补丁 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-18-3 | 验证旧SDK和候选Pi兼容，保留回退构建 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-18-1 可先完成不依赖外部工具的契约/fixture准备，DEV-18-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-18-01 | 现有79项套件与预算失败专项有明确处置；跳过仍单列。 | notRun；无产品通过声明 |
| ACC-18-02 | 模型超时后未知调用保留、已知未发取消释放预留。 | notRun；无产品通过声明 |
| ACC-18-03 | Pi升级导致私有方法变化时兼容测试失败，科学包无需改源码。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

