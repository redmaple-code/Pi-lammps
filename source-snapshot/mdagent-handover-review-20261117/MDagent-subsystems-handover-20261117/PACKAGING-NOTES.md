# 打包改动与取舍说明

> 本包不是任何一个上游分支的纯快照。基线是 `MDagent-evidence-v3@feat/ukb-evidence-v3-design-review`（工作树，commit `3fdb6f3`），叠加 `MDagent-src@codex/dual-gate-handoff`（工作树，commit `d7742ad`）独有的前置双门实现。以下逐项说明改动、理由与影响面。

## 1. 源码合并

### 1.1 叠加（不改内容）

从 `MDagent-src` 取入、与 v3 基线不冲突的文件：

| 文件 | 用途 |
|---|---|
| `src/mcp/md_proj_mcp.py` | 前置双门受控写 MCP（9 工具、五道写闸、R-VERIFY） |
| `src/mcp/design_review.py` | A–I 八层审阅与仲裁流水线 |
| `src/mcp/physics_gate.py` | 纯度量层（dmin/组分/结构指纹/task_grade） |
| `src/mcp/mdproj_config.json.example` | 项目根配置样例 |
| `src/mcp/review_experience/`（v3 已有同内容） | 经验包，v3 版本已含全部包 |
| `src/templates/evidence/*.schema.json` | 决策/工件/状态 schema |
| `src/tests/test_{design_review,physics_gate,md_proj_mcp,model_research}.py` | 双门测试 |
| `src/tests/e2e_dual_gate_demo.py` | 五场景端到端（含 fail-closed） |
| `src/scripts/check_persona_sync.py` | 人格双入口一致性校验 |

### 1.2 补入 v3 缺少的运行时依赖

| 文件 | 原因 |
|---|---|
| `src/dashboard/gmx_scan.py` | `src/mcp/md_mcp.py` 顶层 `sys.path.insert(.../dashboard)` 后 `import gmx_scan`；缺它时证据 API/Web UI 测试 5 项报 `ModuleNotFoundError`。 |
| `src/tools/__init__.py` | `tests/test_kb_registry_extension.py` 用 `from tools.kb import …`；缺少后 `ukb/tools` 会以同名常规包遮蔽命名空间，出现 `No module named 'tools.kb'`。 |

### 1.3 恢复 `model_research` 的 `design` 模式

- **问题**：v3 分支的 `src/mcp/model_research.py` 没有 `design` 模式（`MODE_TEMPLATES` / `MODE_RUBRICS` 只有 `select`、`repair`），但同分支 `md_mcp.sim_design_research` 仍调用 `model_research.run(..., mode="design")`，会直接返回错误；`design_review` 的 C2e 设计论证链路也依赖该模式。
- **处理**：从 `MDagent-src` 合并回三段自包含内容：`DESIGN_SUBQUERY_TEMPLATES`、`EVALUATOR_RUBRIC_DESIGN`、对应的 `MODE_TEMPLATES` / `MODE_RUBRICS` 注册。v3 新增的 DeepSeek 中转站、凭证解析、429 回落等改动全部保留。
- **验证**：`test_model_research` 18/18，`e2e_dual_gate_demo` 5/5。

### 1.4 收编 2026-11-15/16 的未提交修复

两个上游工作树在打包时都有未提交改动。以下文件取自工作树当前状态（不是 commit）：

- `src/mcp/md_mcp.py`、`src/mcp/evidence_api.py`、`src/mcp/model_research.py`、`src/mcp/pipeline_check.py`
- `src/mcp/human_authority.py`、`src/tests/test_human_authority.py`、`src/tests/test_md_mcp_contract.py`
- `src/tools/human_approve.py`、`src/scripts/patch_dsh_usage.mjs`、`src/scripts/verify_usage_caliber.py`、`src/tests/test_patch_dsh_usage.mjs`
- `src/dsh-plugins/dsh-md-ops-gate/lib/index.js`、`cordis.patch.yml`

后四项对应 `docs/20_证据系统/方案_证据系统部署断点修复_20261116.md` 中的执行授权、人工审批箱、Agent 使用统计算法修复。

## 2. 为可移植性做的代码/测试修改

| 位置 | 修改 | 理由 |
|---|---|---|
| `src/tests/test_evidence_v3_api.py` | `call()` 显式传 `review_budget`（200000 字节） | 该套件断言未分批的 transport 契约；默认 96KB 预算与请求大小接近，而知识快照包含**绝对安装路径**，安装目录更深会把请求推入分批路径，导致 mock 不匹配。分批行为由 `test_evidence_v3_review.py` 单独覆盖。 |
| `src/tests/test_design_review.py` | 外来包断言加入 `confirmation_surface.json` | 该包 `consumer="evidence"`，是后续新增的证据消费包；原断言是旧清单。 |
| `src/design-review-layer/tests/test_kb_api.py` | 总条目断言 `>100` → `>=45` | 原阈值假定作者的生产库；新播种库合法规模是基线 9 + 常见问题 12 + 审查点 24 = 45。 |
| `src/dsh-plugins/dsh-md-flow-gate/lib/index.js` | `~` 展开改为 `resolve(homedir(), root.slice(2))` | 原字符串拼接在 Windows 产生 `C:\Users\x/mdsim-…` 混合分隔符；修后 27/27。 |

以上四处均不改变被测系统行为，只消除环境/路径依赖。

## 3. 未打包的内容

| 排除项 | 原因 |
|---|---|
| `data/ukb/*.sqlite`、`backups/`、`system_auto.token` | 运行库与凭证；用 `bootstrap` + `seed-all` 重建 |
| `data/memory/`、`data/events/`、`data/ukb_prereview/` | 运行时任务工件与审计账 |
| `mcp/md_config.json`、`*.bak-*`、`ukb_admin.json` | 本机配置与令牌；保留 `.example` |
| `design-review-layer/reports/*.json|*.html|*.md`（顶层生成物） | 可由 CLI 重生成；保留 `reports/discovery/` 输入数据 |
| `tests/e2e_evidence/runs/`、`__pycache__`、`.pytest_cache` | 历史运行记录与缓存 |
| `deliverables/ukb-handover-20261109/` | 旧的单一 UKB 交接包，已被本包取代 |
| `cloud/`、`bio-agent/`、`dashboard`（除 `gmx_scan.py`）、`docs/对抗验证_*`、`case_runs/`、`experiments/` | 相邻系统或历史实跑工件，与四系统解耦 |
| `presets/`、`run-dsh.*`、`install.sh`、前端 bundle | 整机安装与人格入口；需要时从原仓取 |

## 4. 已知的包内差异（不隐藏）

- 包内源码是**工作树状态**，不是 commit 快照；逐文件 sha256 与来源路径见 `PACKAGE-MANIFEST.json`。
- UKB 存储不随包分发：首次使用必须初始化。`verification/RESULTS.json` 中的 6 项 KB 跳过（5 项生产库 + 1 项符号链接）与 1 项 evidence 跳过、1 项双门符号链接跳过，均由该取舍导致。
- `design-review-layer/web/design_plan_server.py` 的 `rt_*` 案例指向 MDagent 工件树；包内没有这些工件，但内置 `case1–case5` 夹具可直接跑通页面与 API。
- `cloud/gate.py`（服务器租户访问闸）未打包；它与四系统的运行时无依赖关系。
