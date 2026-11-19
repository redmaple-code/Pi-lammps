# 设计：方案校核前端迁入 MD Exp 子模块 v0.1

> 日期：2026-11-07。状态：**本地已实现并通过组件级验证；未提交、未部署、未产品验收**。
> 目标：把已定稿的物理一致性校核前端（v1.3 模板）迁入组织库交付的 MD Exp 前端
> （`vendor/deepseek-harness/packages/client/ui-mdexp`），作为计算工作台的子模块。

## 1. 基线确认

| 项 | 事实 |
|---|---|
| 来源 PR | `AetherMindHarness/AetherMind-Official-Site#12`（squash 合并 `3e236a0`，PR head `3f7d6ceb`），标题 “docs: record full source delivery and immutable runtime acceptance” |
| PR12 交付内容 | 把 `vendor/deepseek-harness` 全量源码（19,233 文件）与生产镜像清单一并入库；`ui-mdexp` 是 MD Exp 产品前端 |
| 本地仓库状态 | 本地 `main` 停在 `4c0cecd6`（PR#7 时代），`origin/main` 已到 `55ec6711`（含 PR12 及后续 #20–#55 的 `ui-mdexp` 修复） |
| 本地 vendor 内容 | `vendor/` 为未跟踪副本，`compute.mjs`（本次改动文件）与 PR12 及 `origin/main` 逐字节相同；`index.ts` 比 `origin/main` 旧一个提交（缺 #44 的品牌图标替换），与本模块无关，未顺手覆盖 |
| 定稿模板 | `design-review-layer/web/static/design-plan.html`，manifest 版本 v1.3，sha256 `0130c76cc71388f594b9be9ddef79f59b86058de4912a259f4f87d579254958b`，owner-approved 2026-10-30 |
| 旧尝试 | `ui-mdexp/src/client/DesignReview.mjs`（上一会话遗留的单文件简版）已删除，由本子模块取代 |

PR12 本身只交付源码，不含方案校核功能；本次迁移是在其前端风格基础上新增子模块。

## 2. 落位与结构

```
vendor/deepseek-harness/packages/client/ui-mdexp/src/client/
  compute.mjs                     # 计算工作台；侧栏第三项「方案校核」+ review 分支
  design-review/
    index.mjs                     # 页面状态/加载/步骤跳转/导出/门状态按钮
    parts.mjs                     # 分节/示意图/对照表/审查点/in 文件查验/UKB 抽屉
    styles.mjs                    # 样式，只用 DSH 主题变量
    README.md                     # 子模块契约
tests/design-review.client.spec.tsx # 5 项组件级测试
```

信息架构严格对应 v1.3：溯源条 → 步骤条（① 方案全文 / ② 方案对照 / ③ in 文件查验）
→ 宏观实验示意 → 实验/仿真两栏全文（仿真栏带「设置依据」折叠）→ 对照表（全部/
存疑/合理/不成立筛选 + 跨尺度论证）→ 审查点发现卡片 → in 文件逐行查验（行号高亮）
→ 门状态条（启动计算/刷新/导出）。未新增任何审核规则。

## 3. 风格适配（MD Exp）

- 颜色、边框、底色全部走 `--dsw-alias-*` 变量，亮/暗色共用；不再使用模板的固定
  1360px 浅色画布。
- 保留模板密度但把正文从 9–10px 提到 13px，标题 13–14px，与工作台其它页一致。
- 子模块自带 `md-dr-btn` 按钮原子，不依赖 primitives 包的导出产物；计算工作台容器在 review 页切到 1280px 宽档（`.md-dr-wide`）。
- 标题与按钮使用名词短语/用户语言，不出现工程师式标题或内部门禁术语。

## 4. 只读数据契约

前端经 `accountApi("/hpc/design-review/...")` 读取；服务端
`deploy/wulanchabu/hpc-platform/design_review_router.py` 前缀 `/v1/hpc/design-review`，
由 `mount_hpc` **常驻挂载**（不再依赖环境变量才挂）。导出包解析顺序：`$MDEXP_DESIGN_REVIEW`
→ `/srv/mdexp/design-review/design_review_export.json` → `/srv/mdexp/design_review_export.json`
→ 路由模块同目录。无可用导出包时端点返回 503，前端显示「方案校核数据尚未配置」。

| 端点 | 用途 |
|---|---|
| `GET /cases` | 案例列表 |
| `GET /plan/{id}` | 两案全文、对照、门状态、溯源哈希、示意图 |
| `GET /check/{id}` | 查验条目、anchors、文件/代码片段 |
| `GET /infile/{id}` | in 文件逐行原文 |
| `GET /discovery/{id}` | 审查点与处置计数 |
| `GET /ukbdebug/{id}/{point}` | 单点 UKB 检索溯源 |

导出包由 `design-review-layer/review_layer/export_bundle.py` 生成。本次把该脚本改为
复用 8900 页面的 builder（`build_plan/build_check/build_discovery/build_ukb_debug`），
避免静态包缺 `schematic/provenance_hash/对照论证/file+code anchors` 等字段；
重新生成后 7 案例、720,496 字节。

## 5. 验证证据

| 检查 | 结果 |
|---|---|
| `node --check`（index/parts/styles/compute） | 全部通过 |
| 导出包重生成 | 7 案例；`schematic` 7/7；溯源 7 字段；case1–5 的 `ukbdebug` 26–28 条/案例 |
| FastAPI TestClient（6 端点，rt_a3 + case1） | 6/6 返回 200；`/cases` 返回 7 个案例；默认路径发现与 env 覆盖两条路径都验过 |
| `vitest run packages/client/ui-mdexp` | 38/38 通过（含本模块 5 例）；`storage.client.spec.tsx` 单跑失败系 primitives 包未构建，与本模块无关 |
| 客户端 bundle | **已产出**：`tsc -b`（client+host）通过后 `tsdown --env.DSH_BUILD_FACE client` 生成 `lib/client.js` 242.3 kB，内含 `design-review/{styles,parts,index}` 三段 |
| 真实数据浏览器渲染（1400×1000，case1） | 两案全文 6/9 段、对照 6 项、审查点 27 条、in 文件 92 行、门状态条全部正确；点击 RP-001 打开 UKB 抽屉，显示检索路径/候选 0 条/基线映射/UKB 统计；抽屉横向溢出实测 470=470 |
| 渲染中修正的缺陷 | ① 判定简写 `q`/`bad` 未在词表内，显示成裸字母 → 补齐同义词；② 长材料名/加载描述在固定 viewBox 内互相压字 → 文本移出 SVG，改为三栏说明；③ 抽屉长路径撑破右边界 → 加 `overflow-wrap`/`word-break` |

## 6. 入口层级（本次提升为产品主功能）

- 会话工具条新增并列按钮「方案校核」（`MdAgentDesignReviewNavigation`，`data-dsh-nav="design-review"`），
  与「计算工作台」同级；点击直接进入 `review` 页。
- 弹窗内左侧导航保留「方案校核」项，`jobs`/`review` 可互切；`review` 页宽度 1280px 档。
- 三个入口共享同一弹窗实现（`MdAgentWorkbenchNavigation` + `initialPage`），无逻辑分叉。

## 7. 真实 DSH 前端本地部署（2026-11-09）

按产品真实形态部署，不用演示壳：

1. `tsc -b tsconfig.host.json`（`NODE_OPTIONS=--max-old-space-size=8192`，默认堆会 OOM）
2. `tsdown --config-loader native --env.DSH_BUILD_FACE host`（产出 `vendor/cordis/lib/index.js` 等运行时）
3. `tsdown --config-loader native --env.DSH_BUILD_FACE client`（233 包，零 UNRESOLVED_IMPORT）
4. `pnpm --filter @deepseek-ai/dsh-web-frontend run build`（vite → `apps/web/dist`）
5. `MDAGENT_PRODUCT=mdexp pnpm run start:web -- --port 8899 --host 127.0.0.1 --no-open`
   → 打印带进程 token 的 `http://127.0.0.1:8899/?token=…`

构建顺序关键：client 面必须在 host 面之后，否则 util/cosmokit 等库尚无 `lib/index.js`，
会被 rolldown 当成外部依赖写进 `client.js`，浏览器报
`require("@deepseek-ai/cosmokit") missed the module table`。

宿主只从 `apps/web/dist` 出静态文件，因此把账号客户端与导出包放进
`apps/web/dist/official/`：`assets/hpc/account-client-xk5txs7f.js`（本地只读桩，映射到
`/official/design_review_export.json`）、`design_review_export.json`、空的 interface.css。
真实产品中这些由官网下发；本地无官网网关，故用同路径桩替代。

实测（1500×950）：`__MDEXP_PRODUCT__=true`，侧栏出现 `data-dsh-nav=workbench|design-review`
两个入口；点「方案校核」直接打开工作台弹窗并落在校核页；case1 显示门状态、对照 6 项、
审查点 27 条、溯源哈希、两案 6/9 段全文。DSH 的 DeepSeek 登录弹窗是宿主自身的凭证栅栏，
与校核功能无关，关闭即可。

## 8. 对话内提醒与悬浮校核（2026-11-10）

- **位置**：`conversation.composer.dock` 插槽（输入框下方、仅 `composer` 形态渲染），
  由 `ui-mdexp/src/client/design-review/notice.mjs` 提供。
- **检测**：按 session 记录状态（localStorage `mdexp.design-review.state`）。
  `none` → 浅红提醒「当前对话还没有完成物理一致性校核」+「开始校核」；
  `pending` → 「方案校核尚未通过（当前判定 GATE）」+「继续校核」；`passed` 隐藏；
  另有「稍后」/× 置为 `dismissed`。
- **打开方式**：点击按钮在**当前页**弹出悬浮层（`.md-dr-float-mask/.md-dr-float`），
  不跳转新页面、不复用工作台弹窗；悬浮层带标题、关闭键与滚动内容区。
- **裁决回写**：`DesignReview` 新增 `onGate(gate, caseId)`；加载到 `GATED_PASS` 时
  该会话标记 `passed` 并隐藏提醒，否则保持 `pending`。
- **布局对齐（2026-11-10 修正）**：`composer.dock` 是横向 flex 行，提示条原先与右侧
  「N 轮 N 步」并排而偏移。现改为外层 `.md-dr-slot` 独占整行（`flex:0 0 100%`）并居中
  内层提示条，宽度取 `--dsh-composer-card-max-width`，与输入卡逐像素对齐（实测
  notice 813×61 @x=480 = card 813×98 @x=480）；`[data-composer-dock]:has(.md-dr-slot)`
  才启用换行，不影响无提醒的会话。
- **风格统一**：主按钮改用 `--dsw-alias-button-primary-fill/hover`，边框/底色/文字全部走
  `--dsw-alias-*`；提示条 13px + 16px 图标，悬浮层与产品弹窗一致（16px 圆角、遮罩、标题栏）。

## 9. 边界与遗留

- 未部署：未在生产主机放置导出包、未跑 Nginx/网关实测，未做真实账号登录验收。
- 本仓库工作区有上一会话遗留的未提交改动（`hpc_platform.py` 挂载段、`out/` 构建产物、
  `vendor/` 为未跟踪副本）；本次改动未提交、未推送。
- 「启动计算」仅做 UX 导航；放行仍由服务端 `validate_intent` 与 `sim_exec` 拦截决定。
- 生产部署前需：把导出包放到上述默认路径之一（或设 `MDEXP_DESIGN_REVIEW`）；
  确认网关 `/api/v1/console/hpc/*` → `/v1/hpc/*` 对 `design-review` 生效；
  用真实账号在浏览器里点一遍两个入口。