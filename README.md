# Pi-lammps

基于 Pi-agent 与 Red Maple Agent Base 的跨领域仿真工作区，包含仿真运行时、可修改领域包、用户引导式领域库初始化、开发规范、实际算例与交付包。

## 获取项目

Red Maple 基座、LAMMPS 工作流导出和 MDagent 发布工作区通过 Git 子模块固定版本，首次克隆时一并获取。

```powershell
git clone --recurse-submodules https://github.com/redmaple-code/Pi-lammps.git
cd Pi-lammps
```

已有克隆可执行 `git submodule update --init --recursive`。

## 安装与启动

需要 Node.js 22.19+ 和 Python 3.11。具体科学软件及 Python 依赖见 [仿真运行时说明](simulation/README.md)。

```powershell
Push-Location redmaple-agent
npm ci --ignore-scripts
Pop-Location
Push-Location simulation
npm ci --ignore-scripts
Pop-Location
```

先由本机凭据管理器将模型密钥注入 `AISPOT_DS_API_KEY` 环境变量，再进入 simulation 目录启动交互界面。

```powershell
cd simulation
node apps/cli/main.js agent --config runtime.example.json
```

本机已配置 personal-twin 时，可以在 simulation 目录使用以下注入启动命令。其他机器使用自己的凭据管理器和部署路径。

```powershell
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js agent --config runtime.example.json
```

模型连接配置位于 `simulation/runtime.example.json`，凭据通过 `AISPOT_DS_API_KEY` 环境变量注入。当前客户端窗口为 40,960 tokens，自动压缩启用。进入 Pi 后使用 `/domain-init new` 初始化领域库。

## 使用领域库初始化

首次使用请阅读 [领域库初始化使用指南](docs/guides/domain-library-initialization.md)。指南覆盖环境准备、行业案例与规范注入、经验访谈、知识和标签候选、逐项与批次审阅、检索、调研、导出和跨会话续用。

在 Pi 中可以按以下顺序操作，材料登记后让 Pi 读取来源并整理候选，再进入审阅。

```text
/domain-init new
/domain-init answer
/domain-init source
/domain-init review
/domain-init export
```

指南使用仓库已有的官方 1IEP 重对接案例走完整流程，并说明有限元动力学领域如何接入自己的案例、规范和验证经验。

## 内容入口

| 路径 | 内容 |
|---|---|
| [simulation](simulation/README.md) | 可运行仿真工作流、领域包、Pi 扩展、CLI 与测试 |
| [redmaple-agent](redmaple-agent/README.md) | 保留独立 Git 历史的 Agent 基座子模块 |
| [lammps-workflow-export](lammps-workflow-export/README.md) | 已发布的 LAMMPS 工作流源码子模块 |
| work/mdagent-github-publish-20261118 | MDagent 源码与分发材料子模块 |
| [docs](docs/README.md) | 架构、开发规范、领域包制作规范与实施记录 |
| [领域库初始化使用指南](docs/guides/domain-library-initialization.md) | 从建库到审阅、检索、调研、导出及常见情况的操作步骤 |
| [领域库初始化开发说明](simulation/agent-modules/domain-initialization/README.md) | 模型工具、操作契约、仓储接口与独立装配 |
| [领域工作流](docs/domain-workflows/README.md) | 小分子对接、有限元动力学及通用领域包设计 |
| [上下文与技能检查](docs/implementation/上下文与技能注入专项检查.md) | 40K 配置、压缩预算、技能读取权限及专项回归 |
| mdagent-modules、mdagent-standalone | 迁移模块、独立运行材料与相关资源 |
| source-snapshot | 原始基线与交接源码快照 |
| work、simulation/work | 算例、输入输出、分析结果与过程材料 |
| deliverables | 规范文档包、演示与实际算例交付压缩包 |

源码、文档、算例输出和交付包纳入版本管理。依赖安装目录、运行时数据库、缓存和凭据文件遵循 `.gitignore`。原始计算日志及验证记录可从相应算例目录和实施文档查阅。

## 发布基线

Red Maple 与 MDagent 的原本地仓库采用浅克隆，原远端在本次发布时无法访问。新账号下的对应仓库以完整当前源码建立发布基线，本机原历史分支保留。父仓库固定到已推送的新基线提交。
