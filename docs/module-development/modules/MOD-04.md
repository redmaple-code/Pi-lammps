# MOD-04 K2 检查、独立审查与放行

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

校验检查适用性、审查独立性和放行前置；机器证明结构/关联有效，科学结论仍由相应审查产生。

- 建议责任岗位：流程内核开发；科学审查人员复核规则。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/kernel/src/gates`。
- 当前依据：redmaple-agent/src/workflow-verification.js；autolammps/src/commands/evidence-validate.ts（stub，不能直接复用）。
- 需求主责：MIG-015、MIG-034、MIG-045、MIG-046、MIG-047。
- 通用需求主责：GSF-007、GSF-024。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-06](MOD-06.md)、[MOD-19](MOD-19.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：3–5 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `applicability.ts`：受限三值解释器
- `review-validator.ts`：身份/证据/摘要核对
- `gate-evaluator.ts`：放行或缺口
- `permits.ts`：执行许可引用
- `test/gates/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `evaluateApplicability(expression, facts) → true/false/unknown + factRefs`
- `validateReview(receipt, subject, policy) → ValidReview / diagnostics`
- `evaluateGate(stage, receipts, stateRevision) → GateDecision`
- `issueRunPermit(closure, gateRefs, authorizationRef) → PermitRef`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

触发条件与通过结果分离。unknown 永不转换为 false；机器无法判断时审查者须提供规则允许的适用性裁决，保留原unknown。PASS 需要当前工件、checkSet、规则版本和真实身份；writer/reviewer 不同实例且能力隔离。MB触发违规按照LAMMPS规则 BLOCKED；普通可改问题 REVISE。门禁回执是不可变记录，协调者只能请求推进。输入许可绑定锁、方案、闭包、门和授权，执行前再次核有效性。

## 5. 数据、存储与所有权

保存 CheckResult/ReviewReceipt/GateDecision/PermitRef；GateDecision 指向原审查，不覆盖其 verdict。人工核验带 actor、范围、限制和材料引用，不能以 stub exit 0 代替。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 解析事实与条件，列出适用/不适用/未知检查。
2. 验证独立角色回执、关键证据、必需输出和输入摘要。
3. 返回可提交 GateDecision；K1请求推进、K6提交，不在模块内偷偷修改阶段。

异常与恢复：ARTIFACT_CHANGED、EVIDENCE_INSUFFICIENT、REVIEW_REQUIRED 不自动重试；验证器异常按不放行处理。合法人工回退保留置信度限制，不扩大coordinator读取权限。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-04-1 | 冻结GateDecision/许可与三值语义fixture | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-04-2 | 实现条件解释器和独立审查校验 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-04-3 | 验证所有绕门负例及机器/人工回退 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-04-1 可先完成不依赖外部工具的契约/fixture准备，DEV-04-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-04-01 | pair_coeff存在但映射未知，不能把触发条件算作false。 | notRun；无产品通过声明 |
| ACC-04-02 | PASS回执的inputDigest被修改，门拒绝且旧回执保留。 | notRun；无产品通过声明 |
| ACC-04-03 | MB违规返回BLOCKED；writer伪造reviewer身份和占位校验器均无效。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

