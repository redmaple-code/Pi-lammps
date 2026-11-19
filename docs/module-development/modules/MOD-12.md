# MOD-12 知识检索、入库、记忆与备份

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

保留知识来源、状态、适用范围和经验闭环；检索后端可替换，知识批准不等于仿真批准。

- 建议责任岗位：知识系统开发；知识审阅人员。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/knowledge`。
- 当前依据：redmaple-agent/src/knowledge.js、platform.js；autolammps/src/utils/lammpsKnowledge、lammpsKbPipeline。
- 需求主责：MIG-018、MIG-067、MIG-068、MIG-069、MIG-070、MIG-071、MIG-072、MIG-073、MIG-074、MIG-075、MIG-076、MIG-077、MIG-078、MIG-079。
- 通用需求主责：GSF-011。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-06](MOD-06.md)、[MOD-08](MOD-08.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：5–8 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `query.ts`：领域过滤/别名/多样性
- `ingest.ts`：原料和13类型
- `curation.ts`：裁决/合并
- `memory.ts`：memdir类型与陈旧提醒
- `backends/`：local、legacy MCP、UKB
- `test/knowledge/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `search(query, scope, pagination) → SearchPage with whyRanked/evidenceLines`
- `read/validate(sourceRef) → 实读/有效性回执`
- `propose(change) / review(proposal) / apply(reviewRef, expectedRevision)`
- `backup/restore/rebuildIndex → 对账回执`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

保留13类型、candidate/pending_review/quarantined/confirmed以及独立quality标签；通用知识approved状态和旧confirmed显式映射，绝不混用知识状态与review PASS。query按领域/材料/模型/阶段筛选，同源chunk多样性去重；排名不构成适用性判断。writer/reviewer记录具体情形—错误—修正，kbCurator建议，需审项kbReviewer裁决。旧批量confirmed记录入口，不能补造人审。merge前重读section，CAS写入；replace精确范围预览。

## 5. 数据、存储与所有权

原始正文/来源修订为权威，索引可重建。memdir user/feedback/project/reference与科学知识隔离；来源撤回/失效仅实现明确支持的链路，不声称全系统自动撤回传播。UKB无固定revision能力则返回unfrozen。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 原料保存和分类→去重/提炼→审阅→受控apply→索引更新。
2. 搜索返回定位、状态、来源组，实际窄读由K4记录。
3. 维护仅输出候选报告；备份包含已提交WAL一致状态，恢复核摘要和状态数量。

异常与恢复：索引故障可窄读原文但必须保留引用；原文缺失不能PASS。知识合并版本冲突不覆盖新编辑；未知后端能力返回CAPABILITY_UNAVAILABLE。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-12-1 | 明确知识类型/状态/检索及后端能力映射 | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-12-2 | 实现入库裁决、合并、memdir和后端适配 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-12-3 | 对账历史库、检索评测与备份恢复，保留未支持边界 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-12-1 可先完成不依赖外部工具的契约/fixture准备，DEV-12-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-12-01 | 13类型4状态导入回读不丢失，quality不自动升为可信等级。 | notRun；无产品通过声明 |
| ACC-12-02 | 两个同源chunk不满足双证据；pending排名高仍pending。 | notRun；无产品通过声明 |
| ACC-12-03 | 并发修改目标section时apply失败且原文保留；备份恢复后查询与来源一致。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

