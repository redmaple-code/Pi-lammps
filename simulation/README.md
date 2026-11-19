# Pi-lammps

基于本地 Red Maple Agent Base 和 Pi 1.5.0 的仿真工作流实现。当前版本 0.1.0 已跑通高熵合金单磨粒磨削的本地真实计算、规则审查、持久执行、分析、Matplotlib/OVITO 绘图和图像核验。

完整开发范围与逐模块证据见 [实施交接](../docs/implementation/README.md)。116 项设计要求的完整验收仍在进行，当前领域包使用 draft/candidateValidation 状态。

## 安装

需要 Node.js 22.19+、Python 3.11 和 LAMMPS。Python 科学环境已在本机实际使用 numpy 1.25.0、scipy 1.11.3、matplotlib 3.7.1、ovito 3.13.1、PyMuPDF。Atomsk 是额外建模适配器，磨削内置 FCC 生成器可独立使用。

在项目根目录运行。

```powershell
Push-Location redmaple-agent
npm ci --ignore-scripts
Pop-Location
Push-Location simulation
npm ci --ignore-scripts
npm test
npm run check
Pop-Location
```

在其他机器安装 Python 依赖时使用独立虚拟环境，并按实际系统安装 LAMMPS、Atomsk 和 MPI。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install numpy==1.25.0 scipy==1.11.3 matplotlib==3.7.1 ovito==3.13.1 PyMuPDF
```

## 本地算例

以下命令在 simulation 目录执行。Python 和 LAMMPS 的默认值从 PATH 查找，也可以显式指定。

```powershell
node apps/cli/main.js grinding --validation --python "C:/Users/redmaple/AppData/Local/Programs/Python/Python311/python.exe" --lammps "E:/LAMMPS 64-bit 2Aug2023-MSMPI/bin/lmp.exe"
```

该命令创建新案例并实际运行求解器，完成后返回 caseId、workdir 和 reportPath。所有选定条件保存在案例 PLAN.json 和 parameters.json。默认条件见 [grinding.parameters.json](domains/lammps/grinding.parameters.json)。用户已经授权由 Pi-lammps 选择本次验证的材料、势与工况。

```powershell
node apps/cli/main.js status
node apps/cli/main.js status CASE_ID
node apps/cli/main.js resume CASE_ID
node apps/cli/main.js analyze CASE_ID
node apps/cli/main.js visualize CASE_ID
node apps/cli/main.js diagnose CASE_ID
node apps/cli/main.js query "高熵合金"
node apps/cli/main.js cancel CASE_ID
```

图像生成后停在 awaitingImageInspection。打开 workdir/figures 中的实际图片，核对帧、区域、方向、单位、图例与研究对象，再按 [检查记录示例](work/grinding-image-inspection.json) 写入观察。确认记录的内容应与实际查看结果一致。

```powershell
node apps/cli/main.js inspect CASE_ID --observations work/grinding-image-inspection.json
```

已有案例修改使用完整参数 JSON，保存新科学修订与独立版本目录。当前修改入口重走该磨削方案的完整计算链。

```powershell
node apps/cli/main.js modify CASE_ID --parameters changed.parameters.json
node apps/cli/main.js resume CASE_ID
node apps/cli/main.js execute CASE_ID
```

execute 入口复用已审输入建立独立 run，返回 completedScope=execute。analyze、visualize、diagnose 的 CLI 实现目前适用于此磨削工作流。

## Pi 交互入口

模型与本地工具路径使用 [运行时配置](runtime.example.json)，凭据通过 personal-twin 注入进程环境。

当前 Pi-lammps 默认使用 **aispot-ds 的 dsv4.1f**。服务模型目录中的正式请求 ID 为 **deepseek-v4.1-flash**，已写入配置并限制为唯一允许模型。端点取自本机 personal-twin L2 台账。密钥由 twin inject 注入 AISPOT_DS_API_KEY，凭据管理遵循 personal-twin 规约。

```powershell
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js agent-check --config runtime.example.json
```

allowedModels 在发送前核对提供方和模型身份，其他模型调用会被拒绝。端点或凭据缺失时返回明确错误。当前客户端容量预算设为 40,960 tokens（40K），单次最大输出 16,384 tokens，默认 low 推理档。自动压缩启用，预留 18,432 tokens，近期上下文保留目标为 8,192 tokens。当前占用超过 22,528 tokens 时触发阈值检查，压缩后的容量校验包含系统提示、技能目录、工具定义、近期消息和下一次输出预留。DeepSeek 的 thinking 与 reasoning_content 兼容字段已配置。费率尚未登记，本地 SDK 的零费率占位值用于兼容传输，实际费用以供应商账单为依据。

```powershell
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js agent --config runtime.example.json
```

原生 TUI 中使用 /sim 查看案例，/sim-grinding 执行本地磨削验证。扩展还注册 simulation_status、simulation_search、simulation_plan。RedMapleRuntimePort 提供隔离角色会话、实读证据、工件发布与独立审查提交。

当前磨削主链由领域程序与独立宿主检查器执行。实际 Pi SDK 会话和工具权限已通过确定性流验证，云模型参与的全角色语义编排、真实论文审计质量尚待提供方环境验证。reproduce CLI 当前交付论文可复现性审计结果。

## 领域库初始化

Pi 会话内使用 `/domain-init new` 创建领域库，按提示说明任务并注入行业案例、规范或亲历经验。`/domain-init` 打开操作菜单，`/domain-init review` 审阅候选，`/domain-init review-all` 可批次选择采用用途。`/domain-init research` 将当前缺口交给 Pi 继续调查。

三个新增工具为 domain_library、domain_library_interview、domain_library_review。知识与标签先作为候选保存，用户通过宿主界面决定采用范围。默认数据位于应用本地 stateDirectory 下的 domain-libraries，以可替换的仓储接口持久化。

离线整理可用 `node apps/cli/main.js domain-init list` 及 new/source/propose/query/export。完整步骤、字段与接入接口见 [功能块说明](agent-modules/domain-initialization/README.md)。

## 状态与恢复

- 权威 SQLite 默认位于 %LOCALAPPDATA%/RedMaple/simulation/WORKSPACE_ID。WORKSPACE_ID 来自绝对工作路径的摘要。
- 使用 --state DIRECTORY 指定固定状态目录，后续命令继续使用相同值。
- 工件对象位于 work/artifacts/objects，案例、工具源快照和方案位于 work/cases，作业位于 work/jobs。
- 本地 supervisor 使用独立隐藏进程。CLI 结束后，已提交的求解器继续运行。resume 使用原 operationId 对账。
- unknown 表示结果仍需对账。先检查原 work/jobs/OPERATION_ID/request.json、receipt.json、日志和实际进程，核实结果后再恢复。
- 备份同时保存两个数据库的一致快照和工件文件。两个数据库间的联合快照应在应用空闲时执行。

```powershell
node scripts/export-evidence.js --output work/delivery-snapshot
```

恢复时将导出的 simulation.sqlite、knowledge.sqlite 放入一个新的本地状态目录，保留原工作区文件和对象目录，再以 --state 指向它。跨路径搬迁需要处理数据库中的绝对 locator 和工作区身份，当前导出用于原路径灾难恢复及审计。用新目录恢复后先运行 status 并核对导出 manifest 的文件摘要。

## 扩展位置

| 目录 | 扩展点 |
|---|---|
| packages/contracts | 版本、身份、记录与包 schema |
| packages/kernel | 状态、证据、审查、修复、研究活动 |
| packages/artifacts | 工件、输入闭包与物化 |
| packages/pack-resolver | 声明解析、能力绑定、版本锁与发布 |
| packages/runtime-redmaple | Pi/Red Maple 会话适配与扩展工具 |
| packages/execution | 本地、Slurm、Atomsk 适配 |
| packages/knowledge | 来源知识、历史索引、后端协议 |
| packages/improvement | 候选、配对评测、应用与回滚 |
| packages/application | 案例装配、磨削与部分入口 |
| domains | 领域声明、规则、资产与归档模板 |
| science | 实际科学计算程序 |

实现使用 ESM JavaScript，与当前 Red Maple 仓库保持一致。原模块文档中的 TypeScript 文件名属于规划，当前源码通过 AJV 边界校验及 node --check 核验。后续可沿公开端口引入 TypeScript、其他求解器或候选 Pi 版本。

## 已完成算例

主算例为 hea-grinding-8b2245c6-ab13-4ef0-be68-85483edce49d，11,520 原子、8,000 步。完整条件、测量、图片和数据入口见 [算例交付报告](../docs/implementation/高熵合金磨削测试.md)。势函数属于理想化 FCC 模型合金，磨粒为解析排斥球。数值收敛和实验物理验证保持 insufficient 状态。
# Agent 可插拔模块

知识、证据、门禁和 LAMMPS 静态一致性检查提供独立安装包和注册/卸载接口，使用宿主提供的身份、资源授权及审计：[模块包和接口规范](agent-modules/README.md)。UKB、其他 agent 宿主和真实生产执行尚未完成验证。

# ROLE-2 角色工作流

2026-11-18 修订接通 grinding、run、resume 和 /sim-grinding 的真实 DS 角色链。工程范围及验证记录见 [AI002–007 工程修订](../docs/implementation/AI002-007工程修订.md)。

在 simulation 目录通过凭据注入运行。

```powershell
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js grinding --validation --config runtime.example.json
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js resume CASE_ID --config runtime.example.json
```

状态查询和图片核验使用既有 status、inspect 命令。角色主链在 awaitingImageInspection 保存图形草稿和当前绑定，实际查看图片后提交核验记录即可完成流程。流程完成、集成指标、数值验证、模型验证和科学目标结果分别呈现。

runtime.example.json 的 simulation 部分提供 taskModelCalls、roleMaxOutputTokens、reviewMaxOutputTokens、roleTokenBudget 和 recordKnowledge。默认任务调用上限 80 次，生产角色输出预留 8192 token，审查预留 16384 token，角色累计 token 上限 512000。当前客户端上下文预算为 40960，自动压缩预留 18432、最近内容保留 8192。服务模型目录未提供容量字段，实际调用的覆盖范围由回执记录。recordKnowledge 启用知识协调、提议和独立裁决分支。实际费率仍由服务账单确定。

providerConnection.transport 选择 curl 时，模型请求经本机 cURL 传输，proxyEnv 指定代理环境变量。认证头和请求正文通过子进程 stdin 传递。安装其他宿主时可选择原生传输，模型约束及审查接口继续沿同一配置加载。

失效后 resume 根据当前阶段、attempt、工件摘要和审查回执恢复，已有完成快照经过当前状态及字节校验才可复用。已确认的外部求解操作通过原 operationId 对账。未知模型调用及其宿主工具效果各自保留恢复记录。

