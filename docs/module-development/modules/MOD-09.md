# MOD-09 K7 指标、科学验证与任务验收

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

确保指标可计算、来源明确，分开运行成功、数值验证、模型验证与任务判据。

- 建议责任岗位：科学计算开发；领域验收人员复核。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/kernel/src/measurement 与 simulation/packages/analysis`。
- 当前依据：原设计§4.6/7；docs/development-spec/02-contracts.md；原data-analyst定义。
- 需求主责：MIG-023、MIG-031、MIG-054、MIG-055、MIG-056、MIG-057。
- 通用需求主责：GSF-014、GSF-015。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-05](MOD-05.md)、[MOD-19](MOD-19.md)、[MOD-11](MOD-11.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：4–6 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `simulation/packages/kernel/src/measurement/definitions.ts`：指标/单位/参考/窗口与规则，不导入科学工具
- `simulation/packages/kernel/src/measurement/output-closure.ts`：运行前输出支持
- `simulation/packages/analysis/src/analysis-receipts.ts`：日志读取区间和计算回执
- `simulation/packages/analysis/src/method-registry.ts`：可信计算方法注册，按包声明绑定
- `simulation/packages/kernel/src/measurement/validation.ts`：numerical/model/task分维度汇总
- `simulation/tests/measurement/*.test.ts`：口径、缺输出、误差与分层结果测试

本节路径相对工作区根，均为计划新增。kernel消费计算回执，analysis适配具体算法/工具，不能让科学工具反写阶段。

## 3. 对外接口

- `checkOutputSupport(definitions, declaredOutputs) → SupportReport`
- `computeMetric(definitionRef, dataRefs, methodBinding) → MetricResult`
- `aggregateValidation(reports, taskCriteria) → AcceptanceSummary`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

MetricDefinition显式quantity/valueType/unit/component/direction/sign/reference/method/selection/sampling/statistics；无量纲用1，确不适用说明原因。运行前逐项列输出列、频率、区间和缺项。分析读取覆盖记录初始化、错误、thermo和尾部；抽样不作全日志无异常承诺。方法在可信计算适配器中，不让领域YAML执行任意代码。D7基准与阈值在方案锁定并绑定用户决定，运行后不改阈值/窗口迎合。新枚举met/notMet/cannotCompute/borderline显式映射原值。

## 5. 数据、存储与所有权

MetricResult引用真实run、数据摘要、定义与方法版本、误差/置信区间、限制；ValidationReport独立保存numerical/model/task三类。校准集与独立验证集分开，未做验证不能标passed。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. WF输入审查前反查每个判据的输出闭合。
2. 对真实run按冻结定义计算；缺数据返回cannotCompute，不补文献数值。
3. 比较标准并报告分项结果与文献工况差异；失败按K5诊断分流。

异常与恢复：缺列/单位/来源为cannotCompute；边界不确定为borderline；运行完但不达标是notMet，不能转成工具失败重试。方法异常保留失败证据。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-09-1 | 定义指标及输出闭合规范与科学fixture | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-09-2 | 实现计算/日志范围/验证报告接口 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-09-3 | 复核单位方向、统计充分性及D7四类结果 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-09-1 可先完成不依赖外部工具的契约/fixture准备，DEV-09-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-09-01 | 加载方向改y且units改变时不沿用x方向或固定换算。 | notRun；无产品通过声明 |
| ACC-09-02 | dump存在但缺所需列/采样间隔不足，运行前闭合检查失败。 | notRun；无产品通过声明 |
| ACC-09-03 | 数值已收敛但任务判据未达标，两个维度独立显示。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

