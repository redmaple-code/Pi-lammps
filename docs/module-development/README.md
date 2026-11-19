# 模块开发文档总入口

> 本目录保留 MD-1 规划快照。当前工程实现和验证覆盖见 [IMPL-1 实施交接](../implementation/README.md) 及 [逐模块记录](../implementation/模块实施记录.md)。tasks.json、requirements.json、source-map.json 中的 planned/notImplemented 描述原规划时点。

工程修订 **MD-1**，2026-11-17。按 project-manager 的范围、责任、依赖、风险、事实与验收分离要求组织；工程细节沿 project-development 接口规范。交付是开发文档，不是产品实现。

现有通用规范已包含包解析、研究活动/耦合和宏观/事件真实验证，因此本次细化为 **21 个模块、63 个实施任务**，覆盖 **92 项 MIG + 24 项 GSF**。保留已有产品来源与编号，不创建额外治理账本、PM state.json或虚构批准回执。

## 阅读顺序

1. [项目实施计划](项目实施计划.md)：范围、工作包、责任、顺序、估算、风险与下一动作。
2. [集成契约](集成契约.md)：模块接口、状态所有权、身份/幂等、跨库事务与恢复。
3. 下表选择模块：实现机制、文件拆分、数据/接口、异常、任务与正反场景。
4. [需求—任务—验收追踪](需求任务验收追踪.md)：逐条回到原始验收，不能只做模块三条最低场景。
5. [文档核验与交接](文档核验与交接.md)：本轮实际文档验证与未实现项。

## 模块目录

| 模块 | 开发说明 | 建议责任 | 实施依赖 |
|---|---|---|---|
| MOD-01 | [公共契约与版本兼容](modules/MOD-01.md) | 契约维护开发 | 基线/契约准备 |
| MOD-02 | [Red Maple 运行时适配与角色任务](modules/MOD-02.md) | Agent 运行时开发 | MOD-01、MOD-18 |
| MOD-03 | [K1 阶段图与任务入口](modules/MOD-03.md) | 流程内核开发 | MOD-01、MOD-04、MOD-08、MOD-19 |
| MOD-04 | [K2 检查、独立审查与放行](modules/MOD-04.md) | 流程内核开发；科学审查人员复核规则 | MOD-01、MOD-06、MOD-19 |
| MOD-05 | [K3 工件、输入闭包与影响追踪](modules/MOD-05.md) | 存储与工件开发 | MOD-01、MOD-08 |
| MOD-06 | [K4 证据与实读回执](modules/MOD-06.md) | 证据系统开发；领域人员审查适用政策 | MOD-01、MOD-05、MOD-08 |
| MOD-07 | [K5 失败诊断与有界修复](modules/MOD-07.md) | 流程内核开发；领域诊断人员复核 | MOD-01、MOD-03、MOD-11 |
| MOD-08 | [K6 权威状态、事务与恢复](modules/MOD-08.md) | 状态与可靠性开发 | MOD-01 |
| MOD-09 | [K7 指标、科学验证与任务验收](modules/MOD-09.md) | 科学计算开发；领域验收人员复核 | MOD-01、MOD-05、MOD-19、MOD-11 |
| MOD-10 | [LAMMPS 领域包与科学规则](modules/MOD-10.md) | LAMMPS领域开发；科学审查人员 | MOD-01、MOD-19 |
| MOD-11 | [求解器与长作业执行适配](modules/MOD-11.md) | 执行与HPC开发 | MOD-01、MOD-05、MOD-08 |
| MOD-12 | [知识检索、入库、记忆与备份](modules/MOD-12.md) | 知识系统开发；知识审阅人员 | MOD-01、MOD-06、MOD-08 |
| MOD-13 | [技能挖掘、角色改进与受控发布](modules/MOD-13.md) | Agent评测开发；资产发布责任人 | MOD-01、MOD-02、MOD-08、MOD-12、MOD-18 |
| MOD-14 | [图形、报告与可复现交付](modules/MOD-14.md) | 科学可视化开发 | MOD-01、MOD-05、MOD-09 |
| MOD-15 | [CLI、TUI 与应用装配](modules/MOD-15.md) | 应用与交互开发 | MOD-01、MOD-02、MOD-03、MOD-07、MOD-09、MOD-14、MOD-19 |
| MOD-16 | [历史导入、资产迁移与兼容](modules/MOD-16.md) | 迁移开发；领域资产维护人 | MOD-01、MOD-05、MOD-08、MOD-10、MOD-12 |
| MOD-17 | [验证平台与完整交付审计](modules/MOD-17.md) | 测试与集成负责人；有权接受者待指定 | MOD-01、MOD-02、MOD-03、MOD-04、MOD-05、MOD-06、MOD-07、MOD-08、MOD-09、MOD-10、MOD-11、MOD-12、MOD-13、MOD-14、MOD-15、MOD-16、MOD-18、MOD-19、MOD-20、MOD-21 |
| MOD-18 | [Red Maple 与 Pi 基座优化](modules/MOD-18.md) | 基座维护开发 | 基线/契约准备 |
| MOD-19 | [领域包解析、能力绑定与版本锁](modules/MOD-19.md) | 包与契约开发 | MOD-01 |
| MOD-20 | [研究活动、有界迭代与耦合交换](modules/MOD-20.md) | 科学工作流开发；耦合模型审查人员 | MOD-01、MOD-03、MOD-05、MOD-09、MOD-11 |
| MOD-21 | [宏观与离散事件领域包及真实基准](modules/MOD-21.md) | 宏观/统计仿真领域开发；各领域科学审查人员 | MOD-01、MOD-09、MOD-11、MOD-19 |

MOD03–09 对应 K1–K7；MOD05 工件可单独成包。MOD20扩展现有K1/K3/K7，不创建第二状态机；MOD21负责真实宏观/事件包，MOD19仅负责通用包解析，二者不能互相代替。

## 可机读追踪

- [tasks.json](tasks.json)：21模块/63任务的规划、角色、依赖与估算，具体人员均未分配。
- [requirements.json](requirements.json)：116项要求各有一个主责模块，包含原验收定位、任务和拟议测试入口。
- [source-map.json](source-map.json)：现有基座与计划文件分开标 existing/planned，不伪称新目录已经实现。
- [文档检查脚本](tools/validate_docs.py)：检查编号、需求集合、任务依赖无环、链接、证据状态和来源新鲜度。

产品功能与验收定义继续使用 [通用开发规范](../development-spec/README.md) 和 [原MIG矩阵](../功能迁移与验收矩阵.md)。本轮未重跑产品79项测试，历史75通过/1失败/3跳过仍以[基线核验](../基线核验.md)为准。

## 变更与完成口径

纯技术细化使用独立工程修订，不修改需求版本。涉及权限、科学判据、数据含义或用户可见行为变化，先定位相关GSF/MIG并记录影响。规划表和模块正文变化须同轮更新追踪；自动生成索引不意味着语义已审阅。

MD-1 形成时所有模块为 **notImplemented / notRun**。实施后以 IMPL-1 的源码和验证记录为当前事实，逐模块剩余范围继续可见。原 tools/validate_docs.py 检查规划基线的新鲜度，实施后的核对使用 [IMPL-1 核验脚本](../implementation/validate.py)。

