# MOD-14 图形、报告与可复现交付

> MD-1 规划快照。当前实现、验证与剩余范围见 [IMPL-1 模块实施记录](../../implementation/模块实施记录.md)。

设计修订：MD-1，2026-11-17。文档已细化；产品实现 notImplemented，验证 notRun。本文接口及新文件均为 planned，不代表已经存在。

## 1. 范围、责任与依据

由既定分析生成实际图形与分项报告，准确显示未完成和限制。

- 建议责任岗位：科学可视化开发。具体人员未分配；不表示已启动代理或已作人员承诺。
- 拟议代码边界：`simulation/packages/presentation`。
- 当前依据：autolammps .angsheng/templates/ovito；knowledge/rules/matplotlib-standards.md；原§7。
- 需求主责：MIG-008、MIG-058、MIG-059、MIG-060、MIG-061。
- 通用需求主责：无独占 GSF；相关要求经依赖模块集成。
- 实施依赖：[MOD-01](MOD-01.md)、[MOD-05](MOD-05.md)、[MOD-09](MOD-09.md)。
- 依赖是开发成果依赖，不是允许相互 import；代码依赖必须服从 [共享集成契约](../集成契约.md) 的单向端口注入。
- 估算：3–5 人日，包含本模块实现与专属测试，不含外部等待；以 [项目实施计划](../项目实施计划.md) 为估算解释和排期依据。

## 2. 文件与职责拆分

- `plots.ts`：matplotlib任务
- `atoms.ts`：OVITO版本接口
- `inspection.ts`：图片核验
- `manifest.ts`：FigureRecord
- `report.ts`：交付清单
- `test/presentation/*.test.ts`：按文件名称承担上文所述职责；实施前确认实际目录

新文件与测试路径均相对上面的模块边界；若跨两个包，实施时在任务回执标注准确路径。现有基座文件只能作为明确列出的改动点，不能把整个基座复制进领域包。

## 3. 对外接口

- `render(plotSpec, analysisRef, toolBinding) → FigureRecord`
- `inspect(figureRef, inspectionInput) → InspectionReceipt`
- `buildReport(caseSnapshot, deliverableRefs) → ReportArtifact`

参数公共头、身份、枚举、权限、幂等、超时、取消和版本服从 [02 数据与接口契约](../../development-spec/02-contracts.md)；本模块的更细约束如下。现有 SDK 名称与拟新增接口须在源码映射中分别标 existing/planned。

## 4. 实现机制与不变量

不重新定义指标、数据窗或D7。数值图和原子图分适配器，图生成后实际打开核对帧、粒子数、ROI、色标与图注。两套OVITO接口分版本绑定，选默认前用小轨迹验证；CNA不等于真实相分数。generated必须有图与核验，scriptOnly/skipped/failed单独呈现。report分开review PASS、run completed、task met、知识confirmed、技能applied，不能笼统全部成功。

## 5. 数据、存储与所有权

FigureRecord绑定run/analysis/data/tool/template/selection与输出digest；render脚本和依赖版本一起交付。报告是只读投影，不可反写阶段。

不复制其它模块权威状态。跨库操作使用持久操作意图及回执，不声称外部调用与本地事务原子提交。关联元数据必须使用真实输入/版本，不能从“最新文件”猜测。

## 6. 主路径与异常处理

1. 从已核分析生成PlotSpec并反查数据与选区。
2. 调用可信工具，收集图片、脚本、错误和manifest。
3. 核验实际图像后生成报告和复现说明；缺能力保留未完成项。

异常与恢复：工具不可用可scriptOnly，必需图未完成不能写generated。数据来源失效使对应图失效；图样式变更只重渲染。

日志仅记录身份、版本、状态、诊断位置和证据引用；敏感源正文、凭证与全历史不进入通用日志。时间/数量等测量保留单位。性能先采基线，尚无已承诺吞吐量或延迟目标。

## 7. 实施任务与交接

| 任务 | 可审阅产物 | 当前状态 | 完成所需证据 |
|---|---|---|---|
| DEV-14-1 | 迁移图模板/OVITO适配及manifest字段 | planned | 接口/fixture/依赖复核记录；不凭文档存在称实现完成 |
| DEV-14-2 | 实现渲染/核验/分项报告 | planned | 实现 commit、实际文件/符号、契约兼容记录与模块检查 |
| DEV-14-3 | 打开真实输出验证语义与复现依赖 | planned | 以下场景的真实回执、环境、限制及消费者交接 |

完整前置任务与归属见 [tasks.json](../tasks.json)；这是本轮规划快照，当前不作为已执行进度。DEV-14-1 可先完成不依赖外部工具的契约/fixture准备，DEV-14-2 开始前需满足相应依赖实现。任务变更必须同步说明受影响消费者、需求和测试。

## 8. 模块验收场景

| 场景 | 输入/动作/可观察预期 | 当前证据 |
|---|---|---|
| ACC-14-01 | 脚本成功但图片缺失不判generated。 | notRun；无产品通过声明 |
| ACC-14-02 | 截面删掉位错/目标区域时核验失败，manifest保留选区。 | notRun；无产品通过声明 |
| ACC-14-03 | 同一报告正确显示completed+notMet+scriptOnly+知识待审。 | notRun；无产品通过声明 |

这些是最低模块场景，不能取代 MIG/GSF 的逐项专属验收。真实模型、科学工具、调度器或外部数据尚不可用时标 notVerified 并说明范围；mock 只关闭其确实证明的机制子项。

## 9. 合并、回退与完成定义

按本模块最小纵切提交，关联 DEV/MIG/GSF。改变外部字段、默认值、权限、状态或科学结论时必须复核消费者和适用规范；产品含义变化按已有授权记录决定，不靠技术重命名绕过。

实现完成需有准确源码入口与契约一致；验证完成需有实际场景回执；产品接受由有权接受者另作决定。数据写入类变更还需可核备份/恢复或新旧版本隔离；不能只回退代码却沿用不兼容数据。回退不得抹去失败、unknown 和用户后续编辑。

当前未关闭的公共外部依赖和决定统一见 [项目实施计划](../项目实施计划.md)，不在本文件复制另一份风险状态。

