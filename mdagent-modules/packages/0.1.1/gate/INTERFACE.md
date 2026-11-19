# 门禁系统接口

包版本 0.1.1。包含原 `md_proj_mcp.py`、`design_review.py`、`physics_gate.py`、阶段规则模块、经验 JSON 和 dsh 插件。依赖 shared 与 knowledge：旧阶段门及 surface 会加载知识适配器；可选真实审阅依赖模型提供方与凭据，`physics_gate` 依赖 numpy。阶段旧状态写入器仅用于历史兼容，不得与证据 v3 对同一任务并行写。

## 前置方案门：原 MCP 工具

组合目录从 `src/mcp` 启动 `python md_proj_mcp.py`；MCP stdio `initialize`、`tools/list` 获取准确 Schema。项目根通过宿主受控 `MDPROJ_ROOT` 配置。

`MCP-TOOLS.json` 是从原 `TOOLS` 提取的静态 Schema 快照；宿主应以组装后的实际 `tools/list` 和服务端校验共同确认版本。

| 工具 | 主要输入 | 效果 / 注意 |
|---|---|---|
| `new_project`, `new_task` | project、case_id 等 | 创建项目与算例；写入本地目录 |
| `draft_intent`, `update_intent` | project、case_id、intent 或 section/patch | 写方案，状态机行为见源码工具 schema |
| `confirm_intent` | project、case_id、confirm_quote | 原实现仅检查文本非空；**不能作为可信人工授权** |
| `validate_intent` | project、case_id 及审阅上下文 | 可调用物理度量与模型审阅；缺依赖时不得伪造通过 |
| `list_projects`, `task_status`, `intent_view` | 项目和算例标识 | 查询；依赖目录解析正确 |

内部可调用 `design_review` 审阅与 `physics_gate` 确定性度量；检索接口和输出类型以实际文件签名为准。完整输入、模型可用性和审阅证据由宿主保证；模型输出本身不等于真实执行许可。

## 阶段门及部署限制

`evolution/stage_gate.py`、`stage_state.py` 及 `surface.py` 为原 E1–E5 机械门和旧状态机制；v3 兼容 MCP 已将状态/判断入口转发至 v3。组合时只选一个任务状态权威；两套历史状态不能共享写目录。

原实现存在已确认问题：`_find_run_dir` 前缀可能串案；`_save_intent` 非原子且无完整 CAS；`_audit` 失败仅记录；`build_brief` 截断；`confirm_quote` 不证明用户身份。拆包未修改这些代码；生产写入/审批及将审阅结论用于自动放行前，必须完成修复与对应对抗测试。dsh 插件仅适用于原 dsh 宿主，不是通用安全边界，其他 agent 需自行接入服务端授权。
