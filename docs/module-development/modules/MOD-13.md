# MOD-13 技能挖掘、角色改进与受控发布

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

从授权历史和失败证据产生候选，经配对评估与明确应用发布；不自动修改活跃受保护资产。

- 建议责任岗位：Agent评测开发；资产发布责任人。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/improvement`。
- 当前依据：redmaple-agent/src/evolution.js；autolammps/src/skills/skillMine；agent-self-evolution-baseline.md。
- 需求主责：MIG-080、MIG-081、MIG-082、MIG-083、MIG-084、MIG-085。
- 通用需求主责：GSF-023。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-02](MOD-02.md)、[MOD-08](MOD-08.md)、[MOD-12](MOD-12.md)、[MOD-18](MOD-18.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：5–8 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `evidence-bundle.ts`：失败/成功/审查来源
- `mining.ts`：范围限定与候选
- `replay.ts`：隔离对照
- `release.ts`：diff/决定/应用/恢复
- `test/improvement/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `collectEvidence(scopeGrant, refs) → EvidenceBundle`
- `propose(bundle, targetRef) → ChangeProposal`
- `evaluate(candidate, baseline, datasetVersion, budget) → EvaluationReceipt`
- `apply(proposalId, userDecisionRef, expectedTargetDigest) → ReleaseReceipt`
- `rollback(releaseRef, expectedCurrentDigest)`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

知识lesson、技能安装、代理行为发布是三条通道，授权不互借。失败回执可成为候选来源，不伪造成功verification。候选含触发、步骤、工具、成功条件、证据片段、频次、作用域；取历史/外发模型范围分别受约束。评估先查科学与权限不退化，再看轮次/失败/回退/上下文/时间/费用；真实模型重复样本与数据集版本固定。仿真profile禁自动激活受保护agents/prompts/rules/hooks。文件回滚必须核当前摘要，不能把资产指针回滚当文件恢复。

## 5. 数据、存储与所有权

记录候选hash、source evidence refs、baseline hash、dataset/evaluator/model版本、逐case配对结果、预算与发布决定。候选副本只写staging；用户后续编辑导致apply/rollback冲突须保留。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 从授权范围提取可核问题，生成候选副本与预期收益。
2. 隔离workspace回放与真实模型评估，保存不退化判定及限度。
3. 明确用户选择后精确应用、写回执；拒绝/编辑候选分别留历史。

异常与恢复：证据失效或基准变更需重评；超预算停止并保留checkpoint；候选无收益保持待审。目标文件变化不强制覆盖。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-13-1 | 扩展EvidenceBundle并迁移技能staging语义 | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-13-2 | 实现隔离配对评测和人工发布事务 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-13-3 | 验证失败来源、自动激活阻断、覆盖冲突与恢复 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-13-1 可先完成不依赖外部工具的契约/fixture准备，DEV-13-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-13-01 | 失败诊断生成候选，源步骤依旧failed，活跃prompt不变。 | notRun；无产品通过声明 |
| ACC-13-02 | 候选便宜但漏review门被拒；历史固定回答回放不算模型改善。 | notRun；无产品通过声明 |
| ACC-13-03 | 安装后用户改技能，再卸载/回滚不删除用户编辑。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

