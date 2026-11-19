# 知识系统：UKB 接口

包版本 0.1.1。职责：知识库 SQLite、版本化条目/Blob、分类注册表、提议/预审/批准、分级检索和审计。包含原始 `src/ukb/`、`src/evolution/kb/`、`src/tools/kb/` 与领域种子。依赖 shared；独立 UKB 本体正常工作无需真实求解器或模型。

原 `evolution/kb/adapter.py` 的证据快照、证据候选提交、发布重验与相应集成测试还依赖 evidence 包；仅组合 knowledge 时这些跨系统功能不可用。要使用它们，请组合 evidence；不要把可选集成能力误称为独立 UKB 的基础能力。

## 现有接口（原生，不是新造协议）

从组合目录 `src/ukb` 的工作目录导入 `from ukb.api import UKB`。`UKB.init(root, packs=(...), actor=..., journal_mode='DELETE')` 创建新库，`UKB(root)` 打开，`close()` 释放。`root` 是绝对数据目录而非源码目录。

| 调用 | 参数与输出 | 副作用 / 约束 |
|---|---|---|
| `overview(profile_id, facet=None, within=None, parent=None)` | 访问口径和分类范围；返回分类概览 | 查询 |
| `catalog(profile_id, scope=None, types=None, limit=None)` | 过滤和上限；返回条目目录 | 查询；目录不是原文 |
| `read(profile_id, scope=None, entry_refs=None, types=None, cursor=None, view='full')` | 条目 ID 或固定版本引用；返回符合 profile 的内容 | 查询；通过引用回查版本，不把摘要当原文 |
| `read_original(profile_id, ref, path, section=None, page=None, lines=None, cursor=None)` | 具体文档及读取范围；返回原文片段 | 原文依赖 blob 存在 |
| `propose(payload, channel, submitter)` | 类型、内容、来源等；返回条目及审核任务 | 写候选；不直接批准 |
| `prereview_bundle(task_id,page=0)` / `prereview_submit(task_id,reviewer_id,page_tokens,opinion)` | 审查页与审查意见 | 独立审查的实际身份须由宿主认证 |
| `decide(task_id,admin_id,token,decision,rationale,bound_hash,channel,args=None)` | 管理员凭证和绑定摘要 | 特权写；不得暴露给普通模型工具 |
| `status()` / `lint()` / `verify_audit()` | 状态、引用/完整性、审计链结果 | 维护检查 |
| `backup(dest_path)` | 不存在的目标文件路径 | SQLite 快照，**不包含 blob**；不能单凭此文件宣称完整恢复 |

实际模型和字段以 `src/ukb/ukb/api.py`、`core/schema.sql` 与 `src/ukb/README.md` 为权威。已有 Agent MCP 入口：组合目录 `src/ukb` 下 `python -m ukb.mcp_server --root <绝对UKB目录> --agent writer`。stdio 的具体工具 Schema 以 `tools/list` 为准。管理员决策不通过普通 Agent 工具。

`MCP-TOOLS.json` 是从 UKB MCP 代码提取的静态工具 Schema；不同 profile 的可见性以实际服务为准。

## 身份、并发、错误与恢复

写事务由 UKB SQLite 管理；批准绑定 `bound_hash`，内容变化导致旧批准失效。`UKBError` 按原代码分类；调用方应显示代码并按错误实际处理，禁止把任意失败当作可重试。知识状态 `candidate/trial/active/...` 保留原义，不映射成科学通过。备份恢复需另行复制并校验 DB 引用的全部 blob、审计链和 schema；凭据独立管理。

安全边界：依赖包本身不做网络身份认证或跨租户授权；宿主负责 actor、profile、scope、可信目录隔离和日志脱敏。管理员凭据通过用户授权的安全注入方式提供，不进源码或输出。原 UKB 生产库不随包分发。
