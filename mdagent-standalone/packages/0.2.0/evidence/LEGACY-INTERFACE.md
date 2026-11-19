# 证据系统 v3 接口

包版本 0.1.1。保留 `src/evolution/evidence_v3`、`src/mcp/evidence_*.py`、human authority、原 Web UI 和相应测试。为保留原实现的知识快照、阶段策略及原兼容依赖，组合时需要 `shared + knowledge + gate`；这不是无依赖单文件库。

## 实际入口

组合目录把 `src`、`src/mcp`、`src/evolution` 加入 Python 导入路径后，调用：

```python
from evidence_api import dispatch
result = dispatch(args, data_root, action_adapter=None, transport=None,
                  result_verifier=None, knowledge_reviewer=None,
                  human_verifier=None, atom_writer=None, review_budget=None,
                  reviewer_id=None, discovery_budget=None)
```

`args` 是 action 字典，`data_root` 为隔离的绝对目录；其他对象是宿主注入能力。返回原有字典/错误，原 action 权威 Schema 为 `src/mcp/evidence_api.py` 中 `TOOL` 与逐分支实现。错误不代表整个事务必然无副作用，尤其外部执行状态未知时必须对账。

`MCP-TOOLS.json` 由原 TOOL 提取，供工具接入时使用；它只有公共 envelope，不能代替逐 action 的条件必填、执行许可及服务端语义校验。

| 操作 | 主要输入 / 输出 | 语义与限制 |
|---|---|---|
| recognize/retrieve/propose/discover | request/intent/bundle；意图、知识检索或建议 | 模型能力缺失会阻塞相应步骤；建议不是审查通过 |
| init/status/revise/plan | task_id、intent、patch/plan、expected_revision 等 | v3 为任务状态所有者；`revision` 与 `state_seq` 不等价 |
| evidence/proof/measure | 记录或已完成 run、测量契约和输入数据 | 写不可变工件；工件摘要被核验 |
| review/evaluate/advance/resolve | 检查、证据、review/receipt、阶段 | review 可依赖模型；advance 必须核对当前状态和凭据 |
| authorization/authorize/revoke_authorization | 动作、授权引用与当前 revision | 决定、证据、一次性执行许可不同；人类身份由可信宿主核实 |
| finish/complete | run/结果或完成条件 | 真实执行回执必须由可信适配提供 |
| submit_evidence/publish/refresh_knowledge/inspect_publication/recover_publication | 记录、知识快照与发布状态 | 需要 UKB 等实际依赖；失败/崩溃先对账 |
| inventory/migrate | 来源路径/迁移意图等 | 历史导入须隔离、对账，不能补造历史通过 |

`request_id` 在原实现仅对部分动作生效，不能视作全 action 幂等。`expected_revision`、`expected_state_seq` 的适用范围见具体分支；无副作用与可重试均按动作判断。

状态/审计：`<data_root>/memory/tasks/<task_id>/state.json` 为 v3 状态；不可变工件在 `artifacts/<category>/`。不得让旧 stage_state 同时写入同一任务路径；旧值先只读导入。出现 `ARTIFACT_MISMATCH`、`REVALIDATION_REQUIRED`、`REVISION_CONFLICT`、`START_UNKNOWN` 等按原错误处理；未知启动禁止重发，先取得外部回执并对账。`sim_exec` 是原 `md_mcp` 的独立入口，需外部 kit 和可信服务端注册，非本包能保证的能力。

Web UI 和 MCP 是可选入口；宿主须提供认证、细粒度授权、路径隔离和不含秘密的日志。真实模型、执行器和生产知识库未由本模块拆分测试证明可用。
