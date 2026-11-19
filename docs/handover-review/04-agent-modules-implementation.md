# Agent 模块包实施与验证记录

> 该方案是此前的 Pi 工程适配尝试，已被用户后续明确纠正；新的独立原生源码拆包与接口见 [MDagent 四个独立功能模块](../../mdagent-modules/README.md)。以下作为历史实施记录保留，不再作为本次交付目标。

日期 2026-11-17；状态：**可插拔接口与部分后端适配已实现，原四子系统的完整迁移和生产验收未完成**。

## 交付物

| 独立安装包 | 可用操作 | 本工作区的可选后端 |
|---|---|---|
| [knowledge](../../simulation/agent-modules/dist/v0.1.0/knowledge/) | search、read | `KnowledgeService`；并非原 UKB |
| [evidence](../../simulation/agent-modules/dist/v0.1.0/evidence/) | read、assess | `EvidenceService`；不是原 v3 全 action |
| [gate](../../simulation/agent-modules/dist/v0.1.0/gate/) | submit | `GateService`；不是前置双门全能力 |
| [consistency](../../simulation/agent-modules/dist/v0.1.0/consistency/) | inspect | MB-001–009、OUTPUT-CLOSURE；并非交接包的全部 intent 对照 |

四包提供一致的 core 和协议，可单独安装、混用、注册或卸载；[源码与完整接口规范](../../simulation/agent-modules/README.md)、[simulation 适配器](../../simulation/agent-modules/adapters/simulation.js)。`build-bundles.js` 从单一源码构建四个独立安装目录，避免手工维护四份实现。组合包也可整体安装。目标 agent 需提供已初始化的后端、可信身份、资源授权及审计存储；当前没有自动修改任何现存 agent 的工具清单或配置。

## 已落实的约束

- 接口协议版本、能力清单、操作副作用分类、结构化错误、独立注册和卸载；拒绝未知操作及能力。
- 身份由宿主提供，授权函数接收操作与输入，须实施资源级权限检查；证据路径只由宿主可信 resolver 解析。
- 非空且无重复的检查集，空/缺项/unknown 不得 PASS；非法状态拒绝；检查结果不等同科学验证或作业许可。
- 每次有效已注册调用先持久写 started，再授权与调用，最后写 completed/failed；链上仅存摘要元数据。审计无法开始时阻止调用；写操作后审计失败返回 unknown，不盲目重发。
- SQLite 审计链逐次验证历史链，通过本地事务串行追加；外部链头锚定、权限、保留与备份仍由宿主负责，不能保证防截尾。

## 验证和范围

执行 `cd simulation; node --test --test-concurrency=1 tests/agent-modules.test.js`：7 项通过，涵盖独立复制加载、四个独立包跨包组合、宿主拒绝、真实本地 knowledge/evidence/gate 调用和阶段推进、LAMMPS include 产生 REVIEW、空/缺项和非法判定、审计篡改及写后未知状态。临时空目录执行 `npm install --offline --ignore-scripts --no-audit --no-fund` 安装全部四包，再从消费者导入五个入口，成功。

仓库语法检查 `npm run check` 通过；最终模块实现的完整 `npm test` 38 项通过、0 失败、0 跳过。随后对本地知识搜索增加断言并单独重跑模块套件，7 项通过。测试使用临时数据；没有读取生产 UKB、真实外部模型或 HPC 环境。

与 HR-1 追踪的关系：本工作为 HR-W04 的覆盖保护、HR-W06 的通用宿主接口，以及 HR-W07 的只读适配提供了**部分实现证据**；没有关闭任一原 HR 问题。尤其未修复原包的人为确认、旧目录串案、持久化、审阅截断、知识备份恢复或验证器问题。

## 尚待实施

1. UKB 实际进程/API 适配、知识版本/审核迁移及完整 DB+blob 备份恢复；生产知识权限与资源隔离由目标宿主实施。
2. 将 v3 完整状态与单次许可、对账、幂等和现有 K6 单一状态权威适配；门禁与执行端生产联调。当前 `requestId` 仅审计关联，不提供幂等。
3. 交接包 intent↔输入文件一致性引擎、完整审阅输入闭包、原包 HR-01–12 缺陷修复及其各自验收。
4. 每个目标 agent 的实际工具封装、认证/授权、部署回滚和跨语言协议测试；真实服务、科学基准、Linux 文件锁、异常恢复和生产验收。
5. 审计外部锚点及尾部截断检测、独立保管、备份恢复演练；确保审计库放本机受控非同步目录，避免个人数据与凭据进入调用正文或审计材料。
