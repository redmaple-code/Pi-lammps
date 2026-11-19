# 独立模块调用协议

版本 0.2.0；协议 `mdagent-stdio/1`。每个模块是自包含目录，只要求 Python 3.11 标准库；四个包无需安装彼此，也不使用 Pi。每个包内含其现有功能所需的兼容源码副本：这是部署和进程层独立，原有源码之间的耦合并未全部重写成可注入端口。各包必须在独立进程启动，不能把四包的 `src` 同时添加到一个 Python 解释器路径。

## 启动

```powershell
python -I -S run.py --data-root <本机非同步目录> --actor <宿主分配的匿名身份>
```

`--data-root` 和 `--actor` 必填；门禁可用 `--project-root <目录>`，否则只访问数据根下 `projects`。默认只读，确需受控写入时宿主显式加 `--allow-write`。`-I` 忽略用户环境和用户 site，`-S` 禁用 site 初始化；两者一起避免第三方 site-packages 参与。`run.py` 仅将自己包内的实现放入 import path。必须由可信宿主验证调用者身份和资源范围，`--actor` 本身只是宿主声明，不能证明使用者身份。

stdin/stdout 每行一个 UTF-8 JSON，进程保持运行，可顺序处理多个请求。请求和响应：

```json
{"id":"request-1","operation":"describe","input":{}}
{"id":"request-1","ok":true,"result":{"module":"knowledge","version":"0.2.0","protocol":"mdagent-stdio/1","operations":{},"writeEnabled":false}}
{"id":"request-2","ok":false,"error":{"code":"ACTION_UNAVAILABLE","sideEffectState":"none","retryable":false}}
```

id 只允许字母、数字、点、下划线、横线，最多 100 字符；只作关联，**不是幂等键**。`describe` 返回该模块的实际允许操作和读写属性，示例中 `operations` 为简写。未知操作拒绝，`input` 必须是 object 且不允许内部 `action` 覆盖。调用方需逐行解析 JSON，stderr 是诊断通道。取消/超时不证明写操作未发生；`unknown` 必须由后端人工或程序对账，不能直接重试。

## 操作

| 模块 | 原生 adapter 操作 | 输入/输出 |
|---|---|---|
| knowledge | `init`（write）、`overview`、`catalog`、`read`、`status`、`verify_audit`、`propose`（write） | 读接口使用固定 agent profile，参数与 `LEGACY-INTERFACE.md` 原 UKB 函数一致；`propose.input.payload` 为候选结构，提交身份来自 `--actor`。`init` 只创建库，不生成管理员密钥；审批与维护需可信宿主使用原生接口。 |
| evidence | `status/evaluate/retrieve`（read）；`init/revise/plan/evidence/proof/advance/resolve/measure/complete`（write） | `input` 为原 `evidence_api.dispatch` 参数，省略 action；task_id、revision、plan 等按各操作原接口提供。原许可、模型审查、真实执行桥不通过此精简 adapter 暴露，源代码仍在包内。 |
| gate | `measure`、`project_list`、`task_status`、`intent_view`（read） | `measure.input.parsed` 为原解析结果 `{coords:[[x,y,z],...],box:[[lo,hi],... ] 或 null}`；结果含最小成像距离（Å）和算法范围。项目查询参数是 project/case_id。已知有缺陷的原方案写入与人为确认不在此 adapter 开放。 |
| consistency | `inspect`（read） | `input.intent` 为方案对象，`input.infile_text` 为输入文本；返回原检查项与汇总结论。无模型，PASS 不等于科学正确、完整覆盖或执行许可。 |

`LEGACY-INTERFACE.md` 是原功能的接口地图，原服务和更广泛的功能源码仍随包保留，但不代表其自动具备标准库-only 的新适配接口。数据与错误结构以实际原始代码为准；适配器对异常只返回类型代码，不泄露错误堆栈或请求内容。UKB PDF 自动解析已关闭，调用 UKB API 时可显式注入宿主提供的 extractor；否则不得宣称原始 PDF 已解析。

## 审计与信任边界

除 describe 外，已识别操作先将 started 追加到 `<data-root>/module-<模块>-audit.sqlite`，再执行，最后追加 completed/failed。记录包含随机调用号、模块、操作、匿名 actor 摘要、请求摘要、结果摘要、时间与 sideEffectState，不含请求/结果正文。审计失败阻止新操作；写后审计失败报告 unknown。摘要可能被猜测或关联，审计库也需严格访问控制。每次追加先验证历史链，SQLite 事务串行；未提供外部锚定，整个数据库或尾部可被替换、删除而不被本链发现。

权限、跨租户隔离、网络鉴权、可信人工决定、审计外部锚定与密钥注入均由宿主提供。避免把个人联系方式、密码、API key、token 放进请求、日志或同步盘。`--allow-write` 只是静态开关，不是认证系统。原门禁的前缀定位和审计缺陷、原证据系统的 request_id 语义和模型依赖等仍存在；组件不是生产认证或原设计全功能重写。

静态 inspect 不能证明原 LAMMPS 输入的 include/动态变量闭包完整。`measure` 使用标准库对所有原子逐对精确计算，正交盒最小成像与原语义一致，但运行时间 O(n²)，超过 10000 原子拒绝；原来的 NumPy 加速和无 NumPy 只抽前 3000 个原子的回退不再使用。科学正确性仍需领域验证。
