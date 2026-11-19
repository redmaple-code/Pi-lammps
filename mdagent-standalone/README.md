# MDagent 独立、可插拔模块 0.2.0

四个独立运行包：`packages/0.2.0/knowledge`（知识）、`evidence`（证据）、`gate`（门禁）、`consistency`（物理一致性）。每个包只需要 Python 3.11 标准库，不需要安装其他包、第三方 Python 库、Node、Pi 或原 MDagent 工作区。按需把包复制给宿主，进程内只加载该包自带代码。

这里的“无需安装其他模块”指**运行和部署依赖**。为保留原行为，每包自带需要的原共享代码，有些还内嵌知识/门禁兼容实现；原源码的跨系统耦合未全部重写为接口注入。模块间可以通过独立进程接口协作，不能把它解释为所有源码已完全解耦或所有旧功能都已有新接口。

## 最小接入

```powershell
Set-Location packages/0.2.0/consistency
python -I -S run.py --data-root D:/mdagent-local/consistency --actor host-service
```

启动后每行发送一个 JSON，例如 `{"id":"1","operation":"describe","input":{}}`，逐行接收 JSON。`-I -S` 让运行不使用用户环境和第三方 site-packages。宿主示例 [client.py](tools/client.py) 提供进程创建、调用和关闭，不自动重试；各包的 [INTERFACE.md](docs/MODULE-INTERFACE.md) 逐项说明协议、操作、错误与约束。

| 模块 | 标准化操作 | 原源码保留范围 / 关键限制 |
|---|---|---|
| knowledge | 初始化、概览、目录、读取、状态、审计检查、候选提议 | 原 UKB 本体与审核 API 内嵌；自动 PDF 第三方解析关闭，需宿主显式提供解析器；管理批准不经此 adapter 暴露 |
| evidence | 状态、检索、评估、建档、修订、计划、证据、测量和阶段推进等 | 保留原 v3；模型审阅、真实执行桥及部分管理操作未在新 adapter 暴露，需可信宿主另行接入 |
| gate | 度量、项目/任务查询 | 保留原前置门源码；存在已知缺陷的方案写入/人工确认未通过此 adapter 暴露 |
| consistency | 真实 intent ↔ LAMMPS 输入检查与汇总 | 不保证 include/动态变量的完整闭包；PASS 不表示科学验证 |

模块提供机器可读 `MODULE.json`、`SHA256SUMS.json`、`LEGACY-INTERFACE.md`、LICENSE 与 NOTICE。SHA-256 用于完整性检查，不是签名。原资产采用 PolyForm Noncommercial 1.0.0；原版本及逐文件溯源见 0.1.1 交接包。

## 审计与授权

写操作默认关闭；可信宿主明确设置 `--allow-write` 才可启用。每个已识别调用使用各自 SQLite 追加审计，开始记录失败则不执行；写入后无法记录结果则返回 unknown，须对账。日志只保存输入/输出摘要，不保存正文或凭据。宿主仍需提供实际身份认证、资源隔离、人工授权、审计外部锚定和备份。不可把私人联系方式或密钥写入数据根、请求、输出、日志或同步目录。

gate 的最小原子距离计算移除 NumPy 和只取前 3000 个原子的回退，改为标准库对全部原子逐对计算。代价为 O(n²)；adapter 对超过 10000 个原子的输入拒绝，而不是悄悄抽样。原有科学边界与生产限制见 [审计与验证](AUDIT-AND-VERIFICATION.md)。

## 验证

```powershell
python -I -S tests/test_standalone.py
python -I -S tools/verify_original.py knowledge
python -I -S tools/verify_original.py evidence
python -I -S tools/verify_original.py gate
python -I -S tools/verify_original.py consistency
```

验证在独立临时目录进行。真实 LLM、外部求解器、生产 UKB、Linux、完整旧 MCP/UI 和所有历史功能的生产行为没有因此通过。原实现中门禁算例串案、非原子写、审阅截断、可信用户授权和完整 blob 恢复等问题仍需修复，不能以此独立包进行生产自动放行。
