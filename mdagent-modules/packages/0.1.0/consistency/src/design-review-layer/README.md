# 一致性校核嵌入层（design-review-layer）

独立于 `MDagent-src` 的**只读**审核层：先做一致性校核，再做审核步；经验库/证据库通过适配器接口接入（当前为 Null，随后接入）。

## 目录

```
review_layer/
  models.py       # ConsistencyItem / ReviewFinding / ReviewReport
  consistency.py  # intent 字段 ↔ in 文件实现 的一致性引擎（五档判定）
  review.py       # 审核步：R-PROV / R-VERDICT / R-CMD / R-EVID / R-COMPLETE / R-GATE
  sources.py      # 经验源/证据源适配器（Null / MDagent seed / data root）
  cli.py          # 命令行：生成 JSON / MD / HTML 报告
  rules/          # infile_rules.json（命令用途白名单）、review_rules.json（判定词/严重度）
examples/bad_drawing/   # 故意含错的夹具（fix indent 当模具、fix all move），用于审核步自测
reports/                # 生成的报告
layer_config.json       # 经验/证据源开关（当前 null）
```

## Web 交互页（8900）

恢复的原 8900 页面，位于本层 `web/`（不碰 MDagent-src）：

```bash
cd web && python design_plan_server.py 8900
# http://127.0.0.1:8900/
```

- 数据：只读 MDagent 工件（intent.json / in.case / validate）
- 视图：① 方案全文（实验/仿真左右）② 方案对照（含「方案内部矛盾·待修订」）③ in 文件查验（行号高亮）
- 检查器：`review_layer/consistency.py`（零规则审核层）

## UKB 知识库耦合

- 默认读取 `MDagent-evidence-v3/data/ukb`（`REVIEW_UKB_ROOT` 可覆盖）；`REVIEW_KB=legacy` 回退本地 JSON。
- 映射：知识=基线维度（rule 9 条）、经验=常见问题（lesson 12 条）、证据=开放审查点（observation 25 条）。
- 写入：`python -m review_layer.ukb_cli seed-all`（propose→机械预审→管理员批准）；token 在 `ukb_admin.json`（600）。
- 读取：`ukb_cli stats|baseline|common`；`common_issues.load()` 与 `discovery.BASELINE` 优先 UKB。
- 本地 `baseline/`、`common_issues/` JSON 仅作种子/回退，不再作为主读取源。

## 使用

```bash
# 真实 MDagent 工件（只读）
python -m review_layer.cli --case rt_a3_al_npt --mdagent-root ../MDagent-src --out reports --config layer_config.json

# 夹具/任意工件（标注 fixture，审核会阻断）
python -m review_layer.cli --intent examples/bad_drawing/intent.json --infile examples/bad_drawing/in.case --fixture --out reports
```

产物：`reports/<case>_review.{json,md,html}`；退出码 0=OK，非 0=需修订/阻断。

## 判定口径（五档）

| 判定 | 含义 |
|---|---|
| `ok` 合理 | 目标/声明与实现一致 |
| `questionable` 存疑 | 需补充说明或证据 |
| `unproven` 未论证 | 无法核对（如实验类型未声明） |
| `invalid` 不成立 | 微观抽象无法代表实验，或实现与目标直接冲突 |
| `plan_conflict` 方案内部矛盾·待修订 | 方案字段过期/自相矛盾（如目标 NPT、序列 nvt、in 文件 NPT）——**修订方案字段，不是否定仿真** |

## 审核层：当前不注入任何规则

按所有者指示（2026-10-29），审核层**不配置任何政策规则**：
- `review_layer/review.py` 只做一致性条目的**确定性汇总**，不产生 findings、不设严重度、不做来源/完整性判断。
- 未来如需规则，必须先经所有者批准，再以数据文件形式加入并配套测试（见 `review_layer/rules/README.md`）。

结论映射（仅汇总，不是规则）：
- 任一条目 `invalid` → `FAIL`
- 否则任一条目 `plan_conflict` → `NEEDS_REVISION`
- 否则任一条目 `questionable/unproven` → `REVIEW`
- 全部 `ok` → `PASS`

## 回归测试

`python -m unittest discover -s tests -v` —— 10 项，覆盖：
- NPT 目标 + 序列 nvt + in 文件 NPT → `plan_conflict`（不得判 invalid）
- NPT 目标但 in 文件无 NPT → `invalid`
- 时间步/步数不符 → `invalid`
- `fix indent` 用于拉拔、`fix move` 作用于全体原子 → `invalid`
- 汇总映射（plan_conflict→NEEDS_REVISION / invalid→FAIL / 全 ok→PASS）

## 经验/证据源接入（随后）

编辑 `layer_config.json`：

```jsonc
{
  "experience": {"type": "mdagent_seed", "path": "<MDagent-src>/mcp/review_experience"},
  "evidence":   {"type": "mdagent_data", "path": "<data_root>"}
}
```

适配器契约：
- `ExperienceSource.lookup(query: dict) -> list[entry]`：按要素/声明/细节匹配条目，返回条目 JSON（含 id/statement/conditions/status/refs）；引擎把命中的 id 写入 `ConsistencyItem.evidence_refs`。
- `EvidenceSource.lookup(refs: list[str]) -> list[atom]`：按引用取 atoms/scenarios 记录。

接入后 `R-EVID` 将从 info 变成实际校验：非合理项无引用 → major/blocker。

## 边界

- 只读：不写 intent/in 文件、不改 MDagent 代码、不新增知识存储
- 方案"全文"目前由 intent 字段生成（缺段如实标注）；自由论述由 MDagent 的 C0–C6 产出后再接入
- 经验源默认关闭（按所有者指示：经验库和证据库随后介入）
