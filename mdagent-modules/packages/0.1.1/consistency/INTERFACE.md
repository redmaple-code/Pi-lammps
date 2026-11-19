# 物理一致性审查接口

包版本 0.1.1。保留原 `src/design-review-layer`：声明与 LAMMPS 输入的一致性引擎、报告、5 个夹具、页面和规则数据。依赖 shared；UKB 连接可选，需要时额外组合 knowledge 并显式配置相同知识根。

## 原生 Python 接口

在组合目录 `src/design-review-layer` 下：

```python
from review_layer.consistency import build_consistency
items = build_consistency(intent_dict, infile_text)
```

输入为解析后的方案 dict 与输入文件文本字符串；返回 `ConsistencyItem` 列表，字段定义见 `review_layer/models.py`，包含检查项 ID、声明、细节、锚点及 verdict。每项 verdict 为 `ok/questionable/unproven/invalid/plan_conflict`。来源/引用和上下文是否完整需由调用方检查。该函数本身不执行求解器。

报告聚合 `review_layer.review.summarize(report)` 输出 PASS/REVIEW/NEEDS_REVISION/FAIL。0.1.1 修复原汇总器对空列表或未知 verdict 返回 PASS 的问题，改为 REVIEW；CLI 的 PASS 退出码也修复为 0。两处修改及原/新摘要记录在 manifest。必需检查集的完整性和重复 ID 尚未由该聚合器校验，宿主必须另外检查；不应直接将汇总结果当放行条件。已覆盖的检查不能证明完整解析 LAMMPS，include/变量/外部依赖应标未验证。

## CLI / UI

组合目录下：

```powershell
Set-Location src/design-review-layer
python -m review_layer.cli --intent cases/case1/intent.json --infile cases/case1/in.case --fixture
python -m review_layer.template_check
```

`web/design_plan_server.py 8900` 提供本地演示页面；不应将其默认绑定视作生产鉴权。接入 UKB 时 `REVIEW_UKB_ROOT` 必须与知识库根一致；未配置时部分路径会回退本地基线，不能笼统宣称 fail-closed。默认 Null 证据源不等于查询到了空结果。

`PASS` 仅反映已执行的声明与输入对应项，不证明物理真实性、数值收敛、审阅独立性或执行许可。模型/科学审阅与最终验收由其他模块和领域人员独立承担。
