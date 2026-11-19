# Pi 跨领域仿真设计与开发规范

当前开发与实际测试状态从 [IMPL-1 实施交接](implementation/README.md) 开始。可运行代码与命令见 [Pi-lammps](../simulation/README.md)。下方设计和 MD-1 记录保留规划基线，当前模块覆盖与缺口见实施交接。

跨领域开发请从 [开发规范文档包](development-spec/README.md) 开始；总体思路见 [跨领域通用设计](跨领域通用仿真工作流与领域包设计.md)。原 [迁移方案](迁移方案.md) 和 [功能迁移与验收矩阵](功能迁移与验收矩阵.md) 继续作为基座接入及 LAMMPS 包迁移依据，适用边界见 [规范决策](development-spec/00-scope-and-decisions.md)。

| 文档 | 内容 |
|---|---|
| [领域库初始化使用指南](guides/domain-library-initialization.md) | 首次建库、案例与规范注入、访谈、候选审阅、检索、调研、导出、CLI 与常见情况 |
| [官方1IEP典型案例复现与输入输出对照](../work/cases/vina-official-1iep-20261118/README.md) | 官方原始输入和参考输出固定、真实Vina运行、逐姿态匹配、8/16/32预算研究与验收记录 |
| [1IEP结果复核与通俗解读](../work/cases/1iep-imatinib-redocking-20261117/validation-review/README.md) | 原始晶体坐标、第三方姿态合理性、种子一致性及结论范围 |
| [领域库初始化功能块](implementation/领域库初始化功能块.md) | Pi 独立模块、/domain-init、用户访谈、资料注入、知识/标签审阅与持久恢复 |
| [开源工具部署与真实1IEP重对接](../work/cases/1iep-imatinib-redocking-20261117/README.md) | Vina/Meeko/RDKit实际部署、三个种子真实搜索、参考RMSD与结果核验 |
| [用户引导式领域包制作规范](domain-pack-authoring/README.md) | 经验访谈、案例/规范注入、标签审阅入库、调研、包设计映射及存储中立契约 |
| [小分子对接完整桌面演练](demonstrations/docking-tabletop-20261117/README.md) | 构造实验、九阶段交接、虚拟运行、异常恢复及结果分析 |
| [IMPL-1 开发实施与测试交接](implementation/README.md) | 本地可运行实现、21 模块状态、116 项需求关联、真实高熵合金磨削、证据与剩余工程范围 |
| [小分子对接与有限元动力学工作流](domain-workflows/README.md) | 两套领域包、六条任务路线、知识/检查/计量资源及任务表单 |
| [MDagent 交接包审阅与开发文档 HR-1](handover-review/README.md) | 12 项审阅意见、8 个开发工作包、21 项验收；完整复测与边界问题复现，产品修复尚未实施 |
| [开发规范文档包 v1.0.0](development-spec/README.md) | 模块、契约、领域包 Schema、微观/宏观/事件样例、状态机、工作包及 24 项通用验收 |
| [模块开发文档 MD-1](module-development/README.md) | 21份模块说明、63个实施任务、116项需求追踪、共享集成契约和项目实施计划 |
| [跨领域通用设计](跨领域通用仿真工作流与领域包设计.md) | 已确认的知识、工具、流程领域包拆解与跨域边界 |
| [迁移方案](迁移方案.md) | 真实代码基线、架构、角色/流程/数据/工具/知识/改进契约、Pi 优化、实施顺序与缺口 |
| [92 项迁移验收矩阵](功能迁移与验收矩阵.md) | 原设计逐项去向、验收场景、章节覆盖 |
| [基线核验](基线核验.md) | 本轮 75 通过/1 失败/3 跳过、复查结果、源码定位和数据缺件 |
| [原设计副本](sources/完整仿真工作流设计.md) | 用户原始材料，未改写；其中引用的专项文件未随附件取得 |
| [机器可读基线](sources/baseline.json) | 源码提交、环境、附件与 lockfile 摘要、测试结果 |
| [350 项候选资产盘点](sources/source-asset-inventory.json) | 源文件路径与 Git blob OID，均为待审/待迁状态 |

已新增 simulation 本地实现，并对 Red Maple 工具授权钩子和未知请求语义测试作了两处修改。真实仿真、历史资产导入和回归结果见实施交接。完整迁移验收仍在进行。
