# MDagent 子系统交接包（知识库 / 证据系统 / 门禁系统 / 物理一致性校核）

| 项 | 值 |
|---|---|
| 包版本 | `1.0.0`（2026-11-17） |
| 目标读者 | 接手维护或移植这四个子系统的工程师 / AI 会话 |
| 内容 | 源码 + 测试 + 种子数据 + 设计文档 + 验证记录 |
| 运行环境 | Python 3.11+（仅标准库；`physics_gate` 需 `numpy`），Node.js 18+（仅门禁插件测试） |
| 许可证 | 沿用 MDagent 的 PolyForm Noncommercial 1.0，见包根 `LICENSE` |
| 验证状态 | 626 项单元/插件测试全绿，另有 5 个双门端到端场景、MCP/Web 冒烟通过；详见 `verification/RESULTS.json` |

本包把四个系统从 MDagent 大仓里**单独抽出**，保持可独立运行、可独立验证：通用知识库 UKB、证据系统 v3、前置/阶段门禁、物理一致性（方案）校核。包内**不含**运行时数据库、管理员令牌、本地 `md_config.json`、运行日志与生成报告。

---

## 1. 四个系统分别是什么

| 系统 | 一句话 | 唯一写路 | 主要入口 |
|---|---|---|---|
| **知识库 UKB** | 单文件 SQLite 通用知识库，注册表驱动，Agent 逐级暴露检索 | `propose → AI 预审 → 管理员哈希绑定批准` | `ukb/` 包、`evolution/kb/` 兼容层、`tools/kb/`、MCP + 审核台 |
| **证据系统 v3** | 把"执行了"推进到"做得对"的开放核查工作流：证据、授权、审查、测量、推进 | `sim_evidence` 单一入口；状态变更需 `expected_revision` | `evolution/evidence_v3/`、`mcp/evidence_*.py`、`webui/` |
| **门禁系统** | 前置双门（C0–C6 方案设计 + A–I 八层审阅/R-VERIFY）与阶段机械门（E1–E5 + 核查面） | 方案写 `md_proj_mcp`；执行 `sim_exec` 单次许可 + 审计桥 | `mcp/md_proj_mcp.py`、`design_review.py`、`physics_gate.py`、`evolution/stage_gate.py`、`surface.py`、`dsh-plugins/*gate` |
| **物理一致性校核** | 只读比对 `intent.json` 与 `in.case` 实现，五档判定，输出 PASS/REVIEW/NEEDS_REVISION/FAIL | 只读；审核层零规则汇总 | `design-review-layer/`（一致性引擎、CLI、8900 交互页、UKB 检索） |

四者共用的底座：`mcp/review_experience/` 经验包（数据文件、热加载、人工闸维护）、`mcp/paths.py` 数据根解析、`md_mcp` 的 17 个只读/受控工具面。

---

## 2. 包内结构

```
MDagent-subsystems-handover-20261117/
├─ README.md                     本文件
├─ 交接手册.md                    详细架构、代码地图、部署、测试、已知问题、接手清单
├─ PACKAGING-NOTES.md            打包改动清单（相对两个上游分支）
├─ VERSION
├─ PACKAGE-MANIFEST.json         逐文件 sha256 + 来源路径 + 分支 commit
├─ docs/                         分类设计文档（索引见 docs/README.md）
│  ├─ 10_知识库/  20_证据系统/  30_门禁系统/  40_物理一致性校核/  90_来源与核验/
├─ src/                          可运行源码树（导入路径与原仓一致）
│  ├─ ukb/                       UKB 包、种子、域包、测试、维护工具
│  ├─ evolution/                 evidence_v3、kb 兼容层、stage_gate/stage_state/surface/评估等
│  ├─ mcp/                       md_mcp、md_proj_mcp、design_review、physics_gate、evidence_*、review_experience
│  ├─ tools/kb/                  bootstrap / import_legacy / prereview / review_queue / serve …
│  ├─ webui/                     证据系统本地 Web UI
│  ├─ design-review-layer/       一致性引擎、规则数据、案例、8900 交互页、UKB 桥、测试
│  ├─ dsh-plugins/               dsh-md-flow-gate、dsh-md-ops-gate
│  ├─ data/domain/               旧库迁移域注册表种子（mdagent_legacy）
│  ├─ dashboard/gmx_scan.py      md_mcp 的 GROMACS 扫描依赖
│  └─ tests/                     分包测试（见第 5 节）
├─ tools/
│  ├─ run_verification.py        一键跑全部套件，生成 verification/
│  ├─ package_manifest.py        重新生成 PACKAGE-MANIFEST.json
│  └─ verify_package.py          按清单校验包完整性
└─ verification/
   ├─ RESULTS.json / RESULTS.md  最近一次验证（套件、耗时、跳过原因）
   └─ logs/                      各套件原始输出
```

---

## 3. 五分钟上手

### 3.1 准备数据根

包内不携带运行库。选一个数据根（下例为包内 `src/data`，也可用任意绝对路径），后续所有组件共用：

```bash
export MDSIM_DATA_ROOT="$(pwd)/src/data"   # Windows PowerShell: $env:MDSIM_DATA_ROOT="...\src\data"
export MDAGENT_KB=ukb
```

### 3.2 初始化 UKB 并播种设计校核知识

```bash
cd src
python tools/kb/bootstrap.py --admin <你的ID> --data-root "$MDSIM_DATA_ROOT"

cd design-review-layer
python -m review_layer.ukb_cli init --admin <你的ID>     # 写 ukb_admin.json（令牌只显示一次）
python -m review_layer.ukb_cli seed-all                  # 基线 9 + 常见问题 12 + 审查点 24
```

> `ukb_cli init` 默认写入 `src/data/ukb`。若数据根不同，用 `REVIEW_UKB_ROOT=<数据根>/ukb` 指定；令牌文件可用 `REVIEW_UKB_ADMIN_FILE` 移出包外。
> `bootstrap` 与 `ukb_cli init` 二选一即可；两者都建同一个 `<数据根>/ukb`。

### 3.3 跑验证

```bash
python tools/run_verification.py
```

脚本会临时准备一份 UKB 存储供 `design-review` 的写路径测试使用，跑完删除；不污染包内数据。

### 3.4 起三个面

```bash
# 1) UKB 管理员审核台 http://127.0.0.1:8765
cd src/ukb && python -m ukb --root "$MDSIM_DATA_ROOT/ukb" serve

# 2) 证据系统 Web UI http://127.0.0.1:8765（与审核台端口冲突时改 --port）
cd src && python -m webui.server --data-root "$MDSIM_DATA_ROOT" --port 8766

# 3) 方案校核交互页 http://127.0.0.1:8900（内置 case1–case5 夹具）
cd src/design-review-layer/web && python design_plan_server.py 8900
```

MCP 挂载（stdio）：

```bash
python src/mcp/md_mcp.py                 # mdsim：17 个工具（含 sim_evidence / sim_exec / sim_design_research）
python src/mcp/md_proj_mcp.py            # mdproj：方案状态机与五道写闸
cd src/ukb && python -m ukb.mcp_server --root "$MDSIM_DATA_ROOT/ukb" --agent writer
```

---

## 4. 架构速览

```
                       ┌──────────────────────────────┐
                       │  UKB 知识库 (SQLite 单文件)   │
                       │  propose→预审→批准，注册表驱动 │
                       └──────────────┬───────────────┘
                                      │ 只读/候选发布
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
┌───────▼────────┐          ┌─────────▼──────────┐        ┌─────────▼──────────┐
│ 门禁：前置双门  │          │ 证据系统 v3         │        │ 物理一致性校核      │
│ C0–C6 / A–I     │          │ recognize→…→complete│        │ intent ↔ in.case    │
│ stage_gate E1–E5│          │ 证据/授权/审查/测量  │        │ 五档判定 + 零规则汇总│
│ md_proj_mcp     │          │ sim_evidence 唯一入口│        │ design-review-layer │
│ sim_exec 审计桥  │          └─────────┬──────────┘        └─────────┬──────────┘
└───────┬────────┘                     │                             │
        └───────────────┬──────────────┴──────────────┬──────────────┘
                        │                             │
              ┌─────────▼─────────┐          ┌────────▼─────────┐
              │ review_experience │          │ 数据根 / memory   │
              │ 经验包（数据文件）  │          │ 任务工件、审计账   │
              └───────────────────┘          └──────────────────┘
```

设计原则（四个系统共同遵守）：

- **代码度量、AI 判断、经验供智**：代码只做确定性检查与流水线纪律，物理对错由 AI 依据可注入经验判断，经验只落数据文件。
- **fail-closed**：任何内部错误、缺凭证、缺执行适配都明确拒绝，不静默回退。
- **单一写路**：知识写入只走 UKB 审核链；方案写入只走 `md_proj_mcp`；执行只走 `sim_exec` 单次许可 + 审计桥。
- **零新增知识存储**：知识、经验、证据都汇入 UKB / `review_experience` 既有底座。

---

## 5. 验证结果（本包实测）

| 套件 | 命令（包根执行） | 结果 |
|---|---|---|
| UKB 包 | `cd src/ukb && python -m unittest discover -s tests -t .` | 103/103 |
| KB 兼容层/证据源 | `cd src && python -m unittest discover -s tests -t tests -p 'test_kb_*.py'` | 44，6 跳过（无生产库 5 + 符号链接 1） |
| 证据系统 v3 | `… -p 'test_evidence_v3_*.py'` | 218，1 跳过 |
| 门禁核心 | `stage_gate / stage_state / assessment_v2 / config_check / deep_review / evidence_grade / human_authority / md_mcp_contract` | 114 全过 |
| 双门 | `design_review / physics_gate / md_proj_mcp / model_research` | 89，1 跳过 |
| 双门端到端 | `python src/tests/e2e_dual_gate_demo.py` | 5/5 场景 |
| 一致性校核 | `cd src/design-review-layer && python -m unittest discover -s tests` | 29/29（临时播种后） |
| 模板校验 | `python -m review_layer.template_check` | v1.3 PASS |
| UKB 冻结校验 | `cd src/ukb && python tools/freeze.py --check` | 1.0.0 未变 |
| flow-gate 插件 | `node src/tests/test_flow_gate.mjs` | 27/27 |
| 使用统计补丁 | `node src/tests/test_patch_dsh_usage.mjs` | 2/2 |
| MCP 冒烟 | `md_mcp` / `ukb.mcp_server` stdio | initialize + tools/list 通过（17 / 16 工具） |
| Web 冒烟 | 方案校核 8900 / 证据 UI | 200，`/api/cases`、`/api/infile_check` 正常 |

合计：**597 项 unittest + 29 项 Node 测试全部通过**，另有 5 个端到端场景与 4 项冒烟。原始输出在 `verification/logs/`。

---

## 6. 边界与不宣称

- **真实模型调用**：`recognize` / 审阅流水线需要沿用 `model_research` 的供应商凭证链；本包未携带凭证，缺凭证时明确 `fail-closed`，不静默降级。
- **真实求解器执行**：`sim_exec` 需要服务器端已核验的执行适配配置（`evidence_execution_registrations` + kit 审计桥）；默认不可执行并返回 `unsupported-action`。
- **一致性校核的 PASS** 只代表 `intent` 字段与 `in.case` 实现在检查项上一致，**不代表物理结论成立**；审核层按所有者指示不注入政策规则，`findings` 恒为空。
- **经验/证据源**：`design-review-layer/review_layer/sources.py` 的 `ExperienceSource/EvidenceSource` 仍是 Null 接口占位；设计校核的知识读取已走 UKB。
- **Windows 权限**：`chmod 0600` 对令牌文件不生效，部署时用 ACL 收紧。
- **未包含**：运行库 `data/ukb/*.sqlite`、`data/memory`、`data/events`、管理员/系统令牌、本地 `mcp/md_config.json`、生成的 `design-review-layer/reports/*.html|json`（保留 `reports/discovery/` 输入数据）、测试运行记录。
- **未包含的相邻系统**：`cloud/gate.py`（服务器访问闸）、`bio-agent` 的平台门、LAMMPS/GROMACS 运行时、HPC 提交链。它们与这四个系统解耦，按需从原仓取。

---

## 7. 来源与版本

| 来源 | 分支 | commit | 用途 |
|---|---|---|---|
| `MDagent-evidence-v3` | `feat/ukb-evidence-v3-design-review` | `3fdb6f3` | UKB、证据 v3、一致性校核、Web UI、工具链基线 |
| `MDagent-src` | `codex/dual-gate-handoff` | `d7742ad` | 前置双门：`md_proj_mcp` / `design_review` / `physics_gate`、经验包、HANDOFF、设计规格 |

- MDagent 版本口径：`1.6.8+20260921`；UKB 冻结版本：`1.0.0`。
- 两个上游工作树在打包时均有未提交改动（含 2026-11-15/16 的凭证链、人工授权、执行断点修复）；本包取自当时的**工作树**，不是纯 commit 快照。逐文件来源与哈希见 `PACKAGE-MANIFEST.json`。
- 包内对测试/插件做了少量可移植性修复与两代源码合并，完整清单与理由见 `PACKAGING-NOTES.md`。

---

## 8. 维护这个包

```bash
python tools/verify_package.py      # 校验所有文件未缺失、哈希未变
python tools/package_manifest.py    # 修改文件后重算清单
python tools/run_verification.py    # 重跑全套验证
cd src/ukb && python tools/freeze.py --check   # UKB 冻结文件校验
```

治理侧记录：本包的抽取与验证登记在工作区 `.project-to-act/` 的证据与文档索引中。
