# UKB 通用知识库

> **已冻结：版本 1.0.0（2026-11-01）**。`RELEASE.json` 记录全部 79 个文件的 sha256，改动后用
> `python tools/freeze.py --check` 校验，升级需显式 `--version`。设计见
> `.project-to-act/docs/设计_通用知识库UKB数据库设计_v0.1.md`（v0.8）；嵌入 MDagent 的替换方案见
> `.project-to-act/docs/方案_UKB嵌入MDagent替换知识库_v1.0.md`。

这是与旧知识库并行的新库。设计见 `.project-to-act/docs/设计_通用知识库UKB数据库设计_v0.1.md`（v0.8）。实现只用 Python 标准库和 SQLite，核心代码不调用 LLM；装有 `pypdf` 时会自动提取 PDF 文本。

## 快速开始

```bash
cd ukb
python -m ukb --root D:/kb init --admin boss          # 输出管理员令牌，只显示一次
python -m ukb --root D:/kb setting checkout_roots '["D:/work"]' --admin boss --reason "允许复用目录"
python -m ukb --root D:/kb setting intake_roots '["D:/inbox"]' --admin boss --reason "允许读取投递目录"
python -m ukb --root D:/kb load-pack data/domain/<你的域包>   # 具体领域的标签、条件键、高危规则
python -m ukb --root D:/kb serve                       # 管理员审核台 http://127.0.0.1:8765
```

Agent 端通过 MCP（stdio）接入，每个 Agent 用自己的名字：

```json
{"command": "python", "args": ["-m", "ukb.mcp_server", "--root", "D:/kb", "--agent", "writer"],
 "cwd": "<repo>/ukb"}
```

服务端会在初始化时下发 `instructions`，告诉 Agent 做事前要先读库，做完后要总结投递，空闲时承担预审。

## 工作方式

- **投递（人和 AI 都可以，主要靠 AI）**：
  1. Agent 先调用 `ukb_guide`，了解什么值得投、怎么投，以及全部类型、标签说明、文件规则。
  2. 再调用 `ukb_prepare`，传入打算使用的类型和标签，拿回该范围内已有条目的全文和待审投递，由 Agent 自己判断是新建、修订、强化还是跳过。
  3. 最后调用 `ukb_propose` 投递。AI 投递必须带 `origin`（来源种类、出处、证据摘要），每个 Agent 每天有投递上限。
  4. 之后用 `ukb_my_submissions` 查看结果，跟进被退回的投递。
- **AI 预审（所有内容任务强制）**：另一个 Agent 从 `ukb_prereview_queue` 领取任务，读完全部材料后提交意见。材料包括待审内容、来源说明、原文、同范围条目全文，以及带说明的标签树。意见必须覆盖类型、标签、重复与矛盾、审核清单；文档型条目还要核对摘要是否忠实于原文。投递方不能预审自己的投递。预审意见只作参考。
- **管理员审核**：只需管理员一人。在审核台巡检时，队列里已预审的任务会显示 AI 的建议，可以直接点"批准"。也可以打开任务，选择修正后批准（改内容或改标签）、退回、并入已有条目或驳回，备注写不写都行。审核决定与内容哈希绑定；MCP 通道不接受管理员决定。
- **读**：`ukb_overview` 看标签树（slug、名称、别名都能定位），`ukb_catalog` 看范围内的条目目录，`ukb_read` 读全文。案例和文献只返回摘要层，原文用 `ukb_read_original` 按章节、页或行读取，复用文件用 `ukb_checkout` 取到工作区。
- **不入库的文件**：轨迹、重启和检查点文件、压缩包、可执行文件、视频、密钥、数据库等，见 `data/seed/file_deny.json`。另有允许清单 `file_format.json`，未登记的格式一律拒绝。
- **扩展**：类型、分类面、标签、条件键、关系、来源类型、文件格式与角色、口径、高危规则都是注册表数据。通过 `propose-registry` 提交，管理员批准即可，不需要改表结构。

## 测试

```bash
python -m unittest discover -s tests -t .                                     # 97 项
python tools/audit_intake.py <scratch> ../automd-microbench-mini/suite       # 真实内容入库与打标审计（92 项检查）
python tools/live_drill.py <scratch> ../automd-microbench-mini/suite         # CLI / MCP 子进程 / HTTP 审核台演练
python -m tools.prodtest.run <含 prodtest 的目录>/kb                          # 生产测试：注入金属 MD 批次（65 项检查）
python -m tools.prodtest.cleanup <kb> list | retire --admin <id>              # 列出 / 清理注入批次（走审核，改为 deprecated）
```

金属 MD 域数据包在 `data/domain/metal_md/`（标签树、目标性质分类面、条件键、高危规则）。

## 目录

```
ukb/core/       schema.sql db canon errors audit blobs registry typespec
                intake documents contribute review navigate maintenance
ukb/api.py      统一入口（每个写操作一个事务）
ukb/mcp_server.py  Agent 工具面：读、投递、预审（不含管理员决定）
ukb/webui.py    管理员审核台
ukb/cli.py      命令行
data/seed/      通用种子（由 tools/build_seed.py 生成）
tests/          单元与端到端测试（只用虚构词汇）
tools/          build_seed.py  audit_intake.py  live_drill.py
```
