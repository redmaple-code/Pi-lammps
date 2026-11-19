# 领域库初始化功能块

模块 ID 为 `domain-initialization`，通过 ModuleHost 协议注册，可独立装载/卸载。默认运行时提供用户访谈、行业案例/规范注入、知识与标签候选、用户审阅、检索、研究上下文和快照导出。

## Pi 使用

启动项目现有 Pi-agent 后输入以下命令。

```text
/domain-init new
```

填写领域库名称和首个目标后，Pi 会恢复当前目标和待补问题，按制作规范引导后续资料注入。系统提示包含 scholar-apprentice 的目标相关知识拆解、案例路径复原、条件/反例分析和经验回注方法。

```text
/domain-init
/domain-init list
/domain-init status LIBRARY_ID
/domain-init answer LIBRARY_ID
/domain-init source LIBRARY_ID
/domain-init review LIBRARY_ID
/domain-init review-all LIBRARY_ID
/domain-init research LIBRARY_ID
/domain-init export LIBRARY_ID
/domain-init pause LIBRARY_ID
/domain-init resume LIBRARY_ID
```

无参数命令打开当前库的操作菜单。重新启动会话后，可从已保存的库列表恢复。`source` 支持直接填写和本机文件。`review` 展示原文摘录、条件、标签和用途，用户可采用、修改、暂缓或拒绝。`review-all` 对当前可审候选做一次批次决定，版本发生变化时要求刷新。

`research` 将当前目标、来源、问题和缺口送入 Pi 的新一轮工作。实际检索使用该 Pi 会话已配置并授权的工具。所得内容经 source/propose 保存为有来源的候选，用户选择采用用途。

## 模型工具

| 工具 | 行为 |
|---|---|
| domain_library | create/list/status/guide/source/readSource/propose/query/research |
| domain_library_interview | 用宿主界面询问一个当前问题，保存用户直接输入 |
| domain_library_review | 展示候选并收集宿主界面的用户决定，可设 batch=true |

无交互界面的调用返回 userInputRequired 或 userReviewRequired 及待处理内容。工作查询默认返回当前有效采用知识，includeCandidates=true 可查看工作台候选。source 注册文本、来源组、摘要和读取范围。propose 可提供 evidenceRanges，按原始行号保存精确片段。

候选和采用后的内容保留独立修订，采用决定绑定候选摘要和范围。标签与知识在同一次仓储提交中生效。修改候选使旧摘要失效。更正先前用户回答后，既有采用内容标为 needsReassessment，默认工作查询暂时排除这些内容，继续修订后可重新审阅。

## 存储与材料

默认位置为应用 stateDirectory 下的 `domain-libraries`，通常位于本机 LOCALAPPDATA。每个库保存一个原子替换的 JSON 快照，内部含版本化制作交换 bundle、回答历史、提案和操作回执。审计文件仅保存调用身份摘要、内容摘要、阶段和结果码。

业务服务依赖仓储的 read、list、save(document, expectedRevision) 接口。FileLibraryRepository 是当前本地实现，MemoryLibraryRepository 用于内存使用和测试。数据库类型继续由后续部署需求决定。

文本材料支持 UTF-8 的 TXT、Markdown、CSV、JSON/JSONL、YAML、日志和 INP。PDF 文本读取需要 Python 和 PyMuPDF，记录总页数、可读页和缺失覆盖。扫描页可通过用户核对后的摘录继续注入。单次文本上限为 262144 字符，文件读取上限为 8 MiB。

资料入口检查常见联系方式和凭据格式，并限制 personal-twin 的受保护资料路径。资料所有者仍需提供脱敏材料，模式检测的覆盖范围在真实数据使用中继续核验。密钥使用沿现有 twin inject 入口，库内保存去标识的领域资料。

写入锁遇到活跃写入时返回 REVISION_CONFLICT。进程被强制终止留下锁时，返回 LOCK_RECOVERY_REQUIRED，保留原快照。维护者应在确认原 PID 已停止且没有其它写者后清理该库对应的 `.json.lock`，再恢复读取。普通会话重启直接读取已提交快照。

## 独立装配

```javascript
import { openDomainInitialization } from './host.js';
import { domainInitializationExtension, withDomainInitializationPolicy } from './pi-extension.js';

const library = await openDomainInitialization({ directory: localStateDirectory });
const runtimeOptions = withDomainInitializationPolicy({
  ...existingOptions,
  extensionFactories: [
    ...(existingOptions.extensionFactories || []),
    domainInitializationExtension(library)
  ]
});
```

策略适配将三个具名领域库工具加入现有 allowTools，保留原有 profile 和授权回调。用户答复、采用和批次采用在模块内部仍检查宿主提供的 human 身份。模型参数不接受调用者身份。

已有 ModuleHost 可直接注册 createDomainInitializationModule({service})，或在 installSimulationModules 中传入 domainInitialization。卸载该模块不会修改其它功能块。

## CLI

在 simulation 目录执行。

```powershell
node apps/cli/main.js domain-init new --title "结构动力学" --objective "整理瞬态建模和评价经验"
node apps/cli/main.js domain-init list
node apps/cli/main.js domain-init status LIBRARY_ID
node apps/cli/main.js domain-init source LIBRARY_ID --file "case.md" --source-type caseReport
node apps/cli/main.js domain-init propose LIBRARY_ID --input candidate.json
node apps/cli/main.js domain-init query LIBRARY_ID --query "采样"
node apps/cli/main.js domain-init export LIBRARY_ID
```

candidate.json 采用以下结构，sourceIds 使用实际登记结果。

```json
{
  "statement": "待审的领域操作经验",
  "kind": "practice",
  "conditions": ["适用条件"],
  "exceptions": ["已知例外"],
  "sourceIds": ["实际来源ID"],
  "evidenceRanges": [{"sourceId": "实际来源ID", "startLine": 1, "endLine": 3}],
  "tags": ["领域主题"]
}
```

CLI 的这些入口可离线整理材料，用户采用在 Pi 交互入口进行。`--state` 继续指定本地应用状态目录。导出快照保存在库目录的 exports 中，包含 snapshotDigest 和当前制作 bundle。

## 验证范围

测试覆盖模块装卸、用户身份、候选隔离、批次原子提交、精确证据片段、修订和旧摘要失效、幂等、并发、文件恢复、材料边界、PDF 读取、公开 CLI、宿主界面回调及真实 Pi SDK 工具链。

Pi SDK 端到端测试使用受控模型响应驱动真实会话与工具执行。真实行业资料的引导质量、领域判断效果和外部研究服务需在相应实际使用任务中评估。完整领域包自动组装、行业条款专用表单和数据库后端接入可沿制作规范继续扩展。
