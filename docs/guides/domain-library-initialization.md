# 领域库初始化使用指南

这项功能帮助领域专家把行业案例、规范、工具手册和亲历经验整理为有来源、有适用条件、可审阅的知识。用户提供材料与判断，Pi 负责追问、整理、建议标签和查找资料，采用决定由用户在交互界面记录。

领域库可持续补充，整理结果可导出，供后续领域包设计与开发使用。当前存储使用本机 JSON 仓储，数据库产品选型保持开放。

## 在浏览器中使用

安装下文所列 Node.js 依赖后，在 `simulation` 目录启动本机工作台。

```powershell
npm run domain:web
```

打开 [本机领域库工作台](http://127.0.0.1:4176)。左侧可创建或切换领域库。先在「访谈」记录目标和经验，在「资料」上传文件或粘贴原文，再选择来源和原文行号整理候选。进入「候选审阅」核对原文、条件、例外和标签，并记录采用用途。已采用知识可在「知识检索」查询，顶部的「导出快照」会下载 JSON。

网页默认可离线使用。需要「用 AI 起草」时，通过凭据管理器注入模型密钥，并使用运行配置启动。下面是本机 personal-twin 的启动示例。

```powershell
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js domain-web --config runtime.example.json
```

选择原文行号后点击「用 AI 起草」，所选片段会发送给配置的模型服务。生成结果先填入可编辑表单，经「提交待审」保存为候选，再由用户记录审阅决定。「调研简报」可以复制给 Pi 继续检索。

网页、Pi 终端与离线 CLI 在工作区和 `--state` 相同时共享同一领域库。通过「刷新」读取其他入口刚写入的内容。端口占用时可运行 `npm run domain:web -- --port 4177`，再打开终端显示的地址。

## 准备环境

首次获取项目时一并拉取子模块。

```powershell
git clone --recurse-submodules https://github.com/redmaple-code/Pi-lammps.git
cd Pi-lammps
Push-Location redmaple-agent
npm ci --ignore-scripts
Pop-Location
cd simulation
npm ci --ignore-scripts
```

需要 Node.js 22.19+。导入 PDF 时还需要 Python 和 PyMuPDF。领域库的文本整理、访谈和审阅可以先进行，仿真求解软件按具体领域任务准备。

交互式 Pi 需要模型连接。当前示例使用 AISpot 的 `deepseek-v4.1-flash`，连接参数见 `simulation/runtime.example.json`。先由自己的凭据管理器将密钥注入 `AISPOT_DS_API_KEY` 环境变量，然后在 simulation 目录运行。

```powershell
node apps/cli/main.js agent --config runtime.example.json
```

使用本机已部署的 personal-twin 时，可直接执行以下命令。`D:/personal-twin/twin.py` 是这台机器的凭据工具路径，其他机器应使用自己的部署路径。

```powershell
python D:/personal-twin/twin.py inject aispot-ds --env AISPOT_DS_API_KEY -- node apps/cli/main.js agent --config runtime.example.json
```

离线 CLI 整理材料时可直接使用后文的 `domain-init` 子命令，模型连接在启动交互式 Pi 时加载。

## 第一次建库的完整流程

下面使用仓库中已有的官方 1IEP 重对接案例练习。该材料记录了本项目的一次复现及其适用边界，后续行业应用继续结合自己的案例和规范审阅。

### 创建领域库

在 Pi 输入。

```text
/domain-init new
```

界面依次询问领域库名称和首个任务目标，可以填写。

| 项目 | 填写示例 |
|---|---|
| 领域库名称 | 小分子对接方法库 |
| 首个目标 | 整理 1IEP 重对接的输入准备、结果对照和方法适用边界，为下一例方案设计提供依据 |

创建后，Pi 显示 `libraryId` 并启动后续引导。保存这个 ID，可以跨会话继续使用同一个库。下文的 `LIBRARY_ID` 都应替换为实际返回的 ID。

### 补充目标与领域经验

```text
/domain-init answer
```

界面从当前待补问题中让你选择一个，并直接记录你的回答。每次操作处理一个问题，Pi 的后续引导围绕当前缺口推进。

回答时可以说明使用岗位、具体任务、结果用于什么决定、经历过的失败及已有验证记录。资料尚未准备好时，可准确输入 `不知道` 或 `暂缓`，系统会记录缺口并继续其他活动。

口述经验也可以作为资料注入。建议同时说明当时的问题、修改前的方案、实际改动、验证方法、结果和适用条件。

### 注入案例、规范与经验原文

```text
/domain-init source
```

界面依次询问资料类型、资料标题和注入方式。练习时选择以下内容。

| 项目 | 选择或填写 |
|---|---|
| 资料类型 | 行业案例 |
| 资料标题 | 官方 1IEP 典型案例复现记录 |
| 注入方式 | 选择本机文件 |
| 文件路径 | `../work/cases/vina-official-1iep-20261118/README.md` |

该路径相对于 simulation 启动目录，也可以填写绝对路径。登记成功会返回 `sourceId`，Pi 会继续整理这份材料。

其他资料可按以下方式准备。

| 资料 | 建议附带的信息 |
|---|---|
| 行业案例 | 任务、输入、基线、实际改动、验证、结果、失败与迁移条件 |
| 行业或企业规范 | 标准名称、版本、条款位置、适用对象、例外和生效范围 |
| 亲历经验 | 发生背景、观察、采取的动作、效果、反例及仍待验证的部分 |
| 工具手册 | 软件与版本、相关章节、参数意义、限制和原文位置 |

文本支持 UTF-8 的 TXT、Markdown、CSV、JSON、JSONL、YAML/YML、LOG 和 INP。PDF 提取会记录可读页及覆盖缺口，扫描件可提供人工核对后的可读摘录。单文件上限为 8 MiB，单次正文上限为 262,144 字符。较长材料可以按章节或条款拆分登记。

提交前去除个人联系方式和凭据。资料标题、正文、路径和候选内容都会经过当前资料边界检查。

### 让 Pi 形成知识与标签候选

资料登记后，可以直接对 Pi 说。

```text
读取当前领域库刚登记的 1IEP 案例。
整理输入准备、结果对照和适用边界方面的知识候选。
每条候选附上原文行号、适用条件、已知例外和建议标签，提交到待审列表。
保留原始观察、用户判断和你的归纳各自的来源，待核实内容保留缺口。
```

Pi 使用 `readSource` 读取原文，通过 `propose` 保存候选。标签可描述领域主题、方法、对象或失败模式。候选显示来源片段，便于用户回到原材料核查。

```text
/domain-init status
```

状态中可查看已登记来源、候选、回答、缺口和下一轮问题。资料数量增加后，候选仍需要经过整理和提交，才能出现在待审列表。

### 审阅候选并选择用途

```text
/domain-init review
```

选择一条候选，核对原文、表述、条件、例外和标签，再选择操作。

界面中的单条来源摘录最多展示 2,000 字符。看到 `truncated=true` 时，可以让 Pi 按该来源的行号补读完整片段，再记录决定。

| 界面操作 | 记录的用途或结果 |
|---|---|
| 采用为项目实践 | 在当前库所描述的项目范围内使用 |
| 采用为包设计候选 | 作为后续领域包设计与开发的候选知识 |
| 采用为参考资料 | 保留为有来源的参考 |
| 修改候选 | 编辑表述、条件和标签，生成新修订，随后重新审阅 |
| 暂缓 | 记录暂缓决定，之后继续审阅 |
| 拒绝 | 记录拒绝决定 |

采用知识时，标签及其关联会在同一次保存中生效。采用决定绑定当前候选版本、来源及用途，认识状态继续按证据保存，当前采用回执中的 `epistemicStatus` 保持 `unassessed`。

需要批次处理时使用。

```text
/domain-init review-all
```

当前批次选项为全部采用为项目实践、全部采用为包设计候选、全部暂缓或全部拒绝，一次最多处理 50 条。较多候选可逐项审阅，或让 Pi 按主题整理后再逐项处理。

### 检索已采用知识

可以直接对 Pi 说。

```text
检索当前领域库中已经采用的“适用边界”相关知识。
列出结论、适用条件、标签、采用用途和来源引用。
```

Pi 使用 `domain_library` 的 `query` 操作。默认工作查询返回当前有效采用的记录，需要复核的记录会暂时从默认结果中排除。查询采用关键词 AND 匹配及标签筛选，适合输入简短、明确的关键词。

查看待审内容时，可以要求 Pi 用 `includeCandidates=true` 查询工作台。用途和采用状态会继续保留在返回内容中。

### 按缺口开展调研

```text
/domain-init research
```

该命令整理当前目标、已读来源、待补问题和缺口，启动 Pi 的下一轮研究。引导吸收 scholar-apprentice 的知识拆解、案例复原、原文核查、边界与反例分析方法。

实际检索使用当前 Pi 会话已经配置并授权的工具。新资料先登记来源和读取范围，再整理成候选，最后由用户审阅用途。外部检索能力的部署沿所在 Pi 会话配置进行。

### 导出并继续下一阶段

```text
/domain-init export
```

成功后返回导出路径和 `snapshotDigest`。JSON 快照包含版本化制作 bundle、来源、知识、标签、采用决定、回答和操作回执，可交给后续领域包设计与开发环节使用。

当前交付覆盖领域库初始化、经验整理、知识采用和导出。完整领域包组装、行业条款专用表单及其他仓储后端可沿 [领域包制作规范](../domain-pack-authoring/README.md) 继续开发。

## 有限元动力学领域怎样开始

为新的领域单独创建库。例如名称填写“结构动力学经验库”，目标填写“整理隐式瞬态分析中载荷、阻尼、时间步和结果采样的项目经验”。

先注入你已有的工程案例、材料与阻尼参数来源、载荷定义、企业规程和独立性验证记录。请 Pi 按任务、模型假设、实际动作、验证、结果和迁移条件逐条整理候选，再审阅采用范围。

仓库的 [有限元动力学工作流](../domain-workflows/fem-dynamics/README.md) 可作为设计材料登记。该工作流当前标记为 draft，行业阈值和工程适用性继续由用户提供的规范、案例与验证结果支撑。

切换领域时使用库列表选择目标库，或在命令中明确指定其 ID。

```text
/domain-init list
/domain-init status LIBRARY_ID
/domain-init source LIBRARY_ID
```

## Pi 命令速查

各操作省略 ID 时，使用当前选定的库，或通过界面选择。

| 命令 | 用途 |
|---|---|
| `/domain-init` | 打开当前库的操作菜单 |
| `/domain-init new` | 新建领域库 |
| `/domain-init list` | 查看已有领域库及 ID |
| `/domain-init status LIBRARY_ID` | 查看来源、候选、回答、缺口与下一步问题 |
| `/domain-init answer LIBRARY_ID` | 通过界面回答一个待补问题 |
| `/domain-init source LIBRARY_ID` | 注入案例、规范、经验或文件 |
| `/domain-init review LIBRARY_ID` | 逐项审阅候选 |
| `/domain-init review-all LIBRARY_ID` | 对当前可审候选作批次决定 |
| `/domain-init research LIBRARY_ID` | 围绕当前缺口启动调研 |
| `/domain-init export LIBRARY_ID` | 导出当前制作快照 |
| `/domain-init pause LIBRARY_ID` | 暂停当前库的写入 |
| `/domain-init resume LIBRARY_ID` | 恢复当前库的写入 |

暂停后可继续查看状态、查询和导出。重启 Pi 后可通过 `list` 和库 ID 继续原库。

## 离线 CLI

以下命令均在 simulation 目录执行。将 `LIBRARY_ID` 替换为创建或列表操作返回的实际 ID。

```powershell
node apps/cli/main.js domain-init new --title "小分子对接方法库" --objective "整理1IEP输入准备、结果对照与适用边界"
node apps/cli/main.js domain-init list
node apps/cli/main.js domain-init status LIBRARY_ID
node apps/cli/main.js domain-init source LIBRARY_ID --file "../work/cases/vina-official-1iep-20261118/README.md" --source-type caseReport
node apps/cli/main.js domain-init propose LIBRARY_ID --input candidate.json
node apps/cli/main.js domain-init query LIBRARY_ID --query "复现"
node apps/cli/main.js domain-init research LIBRARY_ID
node apps/cli/main.js domain-init export LIBRARY_ID
```

`candidate.json` 保存为 UTF-8 无 BOM，示例结构如下。`sourceIds` 使用资料登记回执中的真实 `sourceId`，行号使用原材料的实际范围。

```json
{
  "kind": "observation",
  "statement": "本次归档的1IEP复现主结果对照已完成，存在分项差异。",
  "conditions": ["当前归档案例", "采用该案例固定输入的复现比较"],
  "exceptions": ["独立随机初态的预测能力继续单独验证"],
  "sourceIds": ["替换为实际sourceId"],
  "evidenceRanges": [{"sourceId": "替换为实际sourceId", "startLine": 3, "endLine": 3}],
  "tags": ["小分子对接", "案例复核", "适用边界"]
}
```

CLI 提交候选后，通过 Pi 的 `review` 或 `review-all` 界面记录用户采用决定。无交互界面的访谈与审阅工具分别返回 `userInputRequired`、`userReviewRequired`，并提供待处理内容。

需要离线查看候选时，创建 UTF-8 无 BOM 的 `query.json`，内容为 `{"includeCandidates": true}`，然后执行。

```powershell
node apps/cli/main.js domain-init query LIBRARY_ID --input query.json
```

## 保存位置、续用与备份

默认 Windows 目录为 `%LOCALAPPDATA%\RedMaple\simulation\<workspace-id>\domain-libraries`。工作区 ID 由绝对路径计算，因此更换项目位置时需要核对使用的状态目录。

如需固定状态位置，Pi 和 CLI 都使用相同的 `--state`。下例采用当前机器上的本地目录，其他机器可以选择自己的目录。

```powershell
node apps/cli/main.js agent --config runtime.example.json --state "D:/PiSimulationState"
node apps/cli/main.js domain-init list --state "D:/PiSimulationState"
```

每个领域库保存为独立 JSON 文件，审计文件为 `audit.jsonl`，导出文件在 `exports` 子目录。会话停止后可备份整个 `domain-libraries` 目录。当前 CLI 提供快照导出，后续快照导入与跨后端迁移入口仍待实现。

## 常见情况

| 情况 | 处理方式 |
|---|---|
| Pi 中找不到 `/domain-init` | 按本指南从本项目 `simulation/apps/cli/main.js agent` 启动，确认已拉取当前代码与子模块 |
| 模型凭据缺失 | 核对启动进程中的 `AISPOT_DS_API_KEY` 及运行时模型配置 |
| 材料已登记，待审列表为空 | 请 Pi 读取该来源并调用 `propose` 提交候选，再打开 `review` |
| 查询结果为空 | 核对库 ID、关键词、采用状态和待复核标记，必要时用 `includeCandidates=true` 查看工作台 |
| `PERSONAL_DATA_RESTRICTED` | 去除材料、标题或路径中的个人联系方式与凭据后重新提交 |
| `EXTRACTION_UNAVAILABLE` | 核对文件格式、UTF-8 编码和 PDF 提取环境，或直接提供核对后的摘录 |
| `SCHEMA_INVALID` | 核对文件大小、正文长度、JSON 格式、真实来源 ID 和原文行号；批次上限为 50 条 |
| `REVISION_CONFLICT` | 重新查看当前状态，刷新后继续操作 |
| `DECISION_SCOPE_MISMATCH` | 重新打开候选，核对其新版本、原文与用途后再决定 |
| `SESSION_PAUSED` | 先用 `/domain-init resume LIBRARY_ID` 恢复写入 |
| 重启后库列表不同 | 核对项目路径、`--workspace` 和 `--state` 是否与原会话一致 |
| `LOCK_RECOVERY_REQUIRED` | 由维护者确认锁所指进程已结束、备份库文件，再按模块说明处理对应锁文件 |

操作契约、独立装配和仓储接口见 [功能块开发说明](../../simulation/agent-modules/domain-initialization/README.md)。设计与治理流程见 [用户引导式领域包制作规范](../domain-pack-authoring/README.md)。
