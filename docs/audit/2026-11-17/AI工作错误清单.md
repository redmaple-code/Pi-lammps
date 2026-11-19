# AI 工作错误与遗漏清单

2026-11-18 状态更新：AI001 的误差由用户接受，AI005 按用户要求暂缓；AI002、003、004、006、007 已完成本轮工程修订与限定范围验证，详见 [ROLE-2 工程记录](../../implementation/AI002-007工程修订.md) 及 [交付回执](../../../simulation/audits/2026-11-18-role-workflow/handoff.json)。以下保留原审计时点的发现，不能据旧状态推断当前源码。

核查日期为 2026-11-17。范围覆盖本聊天的工具调用及结果、现有源码、保留的测试日志、三次磨削运行和本次新增对照。下表列出记录中可以确认的 40 项错误、工程遗漏或验证缺口。来源记录之外的行为仍无法穷尽。环境故障见文末。

## 当前待处理问题

| 编号 | 错误或遗漏 | 影响 | 状态与证据 |
|---|---|---|---|
| AI-001 | 质量核验使用输入声明，未核实 EAM 加载后实际质量 | Ni、Co、Cu 的实际运行质量与 structure-report 不一致。Cu 从 63.55 变为 63.0，变化 −0.865% | 待修复。final.data、势文件头及 [质量核验](../../../simulation/audits/2026-11-17/final-state-audit.json) |
| AI-002 | resume 的缓存命中仅核对 revisionId，遗漏阶段失效状态 | 当前阶段 pending、案例 active 时，仍返回旧 completed | 待修复。已在隔离临时案例重现，[复现结果](../../../simulation/audits/2026-11-17/workflow-probe.json) |
| AI-003 | 真实磨削主链的全角色语义编排未贯通 | 主流程主要由硬编码领域程序与宿主检查执行，13 个角色定义和 SDK 端口尚未形成完整自主研究链 | 未完成。packages/application/grinding.js、runtime-redmaple/index.js |
| AI-004 | 在用户要求完整开发时，以部分实现结束本轮交付 | 用户的完整迁移目标仍有工程工作，上一轮最终答复已披露该状态 | 未完成。IMPL-1 剩余范围及本聊天上一轮交付 |
| AI-005 | 116 项要求仅形成模块关联，缺逐项独立验收证据 | 28 个框架测试无法覆盖全部原始验收场景 | 未完成。requirements-status.json 中各项均为 notFullyExecuted |
| AI-006 | 每道科学审查复用了同一份 indent 案例和 indent 手册 | 证据实读和来源组独立性通过，但势适用性、设计参数及测量方法的内容相关性仍有缺口 | 待完善。grinding.js 的 reviewStage 固定 sources |
| AI-007 | 本算例 D7 仅定义数据有限性、原子数、固定层、接触和行程 | 科学有效性及实验一致性缺少可执行的定量判据 | 待完善。PLAN.json 的 criteria，原报告已标 numerical/model insufficient |
| AI-008 | 给出后半程均值前，缺少统计稳态检验 | 切向均值前后半程相差约 28.7%，温度仍有趋势，点估计的代表性不足 | 本次已核查，稳态与误差估计待补。[分窗统计](../../../simulation/audits/2026-11-17/main-case-audit.json) |
| AI-009 | 没有输出完整磨粒能量和热浴能量交换 | 当前 etotal 无法用于闭合外功、储能、热流和数值误差 | 待补输出。输入无 fix_modify tool energy yes，Langevin 无 tally yes |
| AI-010 | 只汇报移动原子全体平均温度，缺接触区温度时序 | 全体平均约 311 K 掩盖空间温差，不能据此描述接触区温度上限 | 本次从 final.data 速度补算末帧，接触顶层去质心温度约 386.8 K。时序仍缺 |
| AI-011 | 未完成尺寸、时间步长、速度和随机种子的敏感性研究 | 有限尺寸、离散误差、速率效应和统计不确定度均缺定量界限 | 待验证。当前主结果来自一个参数点和一个种子 |
| AI-012 | 原交付缺少相同初态的无磨粒对照 | 位移和温升的加载归因缺少直接对照 | 本次已补一个配对种子。无磨粒最大位移 0.582 Å、加载 2.644 Å，[对照结果](../../../simulation/audits/2026-11-17/control-comparison.json) |
| AI-013 | 未卸载、退刀或弛豫即完成构型图交付 | 当前末帧包含在载弹性变形，永久沟槽、残余塑性和去除量尚无证据 | 待验证。输入在磨粒仍加载时 write_dump/write_data |
| AI-014 | 原报告未披露最小化的实际停机条件和残余力 | 按能量容差停止，最大力分量仍为 0.01017 eV/Å。原设 force tolerance 为 1e-6 eV/Å | 本次已披露。能量准则触发符合 LAMMPS 的停止规则，力收敛状态仍应独立说明 |

## 已在前轮修正的代码错误

| 编号 | 错误 | 实际后果或风险 | 修正证据 |
|---|---|---|---|
| AI-015 | 以只读句柄对工件临时文件执行 fsync | Windows 返回 EPERM，初次测试的 5 项失败由此触发 | 改为 r+。test-results-first.log，本聊天工具记录第 636 行 |
| AI-016 | 将知识平台的内容指纹当作文件字节摘要 | KnowledgeService.apply 误报 Knowledge source changed，初次第 6 项失败 | 改用 hashFile(imported.path)。同上第 636 行 |
| AI-017 | 映射错误反例用固定 LF 行尾字符串替换 | Windows CRLF 输入未被修改，反例仍返回 passed，测试失败 | 去除替换条件中的行尾。本聊天第 656、667 行 |
| AI-018 | 发布路径缺少完整绑定回执核验，生成包的结构检查不足 | 发布测试出现 SCHEMA_INVALID/Invalid domain package | 增加 validated 回执要求及发布后 schema 检查，测试使用独立候选目录。第 958、969 行 |
| AI-019 | 审查提交仅比较调用方传入的 producerInstanceId | 调用方可传入与实际阶段生产者不同的身份，削弱独立审查检查 | 与阶段权威生产者身份绑定。当前 gates.js 和 core.test.js |
| AI-020 | 审查提交未强制覆盖当前完整输出对象 | 可提交其他工件或不完整对象，审查对象和阶段对象存在差异 | 增加完整 subjectRefs 摘要绑定。当前 gates.js |
| AI-021 | 调用方 evidence policy 可弱化阶段要求 | localCaseRequired 等冻结前置可能被绕过 | 强制合并阶段 evidencePolicy。当前 gates.js 及回归反例 |
| AI-022 | 执行仅相信闭包提供的 digest 字段 | 更换 entries 后保留旧 digest 仍可能通过许可校验 | validateClosure 重算内容摘要。当前 kernel/index.js |
| AI-023 | 执行许可未确认每个输入属于已通过阶段输出 | 可以为审查范围外的工件构造执行闭包 | 增加批准工件归属检查。当前 kernel/index.js |
| AI-024 | 许可检查未充分核对案例取消状态 | 取消后仍可申请或使用执行许可 | permit/validatePermit 检查案例状态。execution.test.js 含反例 |
| AI-025 | 子进程 timeout 与授权 wallSeconds 未绑定 | 单次执行时限可超过已授权壁钟范围 | preflight 增加上限检查。execution.test.js 含 21 秒对 20 秒反例 |
| AI-026 | 阶段依赖只接受 passed | 可选阶段正确 skipped 后，无缺失输入的后继仍被阻塞 | 允许已解析的 skipped 前置，输入缺失继续检查。workflow.test.js |
| AI-027 | 全阶段完成推导无条件写 completed | 可能覆盖 cancelRequested/cancelled 状态 | 只对 active 案例推导完成。kernel/index.js |
| AI-028 | invalidation 的历史对象共用可变引用 | 重置当前阶段时可能连带改写旧阶段证据 | 使用 structuredClone 保存历史。kernel/index.js |
| AI-029 | 程序操作去重身份缺分析工具源摘要或阶段 attempt | 更换分析程序、重新绘图时可能复用不适用的旧回执 | 程序请求包含 implementationDigest 和 attempt。grinding.js，第 930、1076 行 |
| AI-030 | 首个主算例创建时未冻结 Python/OVITO 源和参数副本 | 原算例求解输入输出可追溯，分析工具的当时版本证据较弱 | 后续案例增加 tools 源快照和冻结 parameters.json。第 930 行 |
| AI-031 | PDF 判断全文时把自行插入的页标题计入正文 | 空白或扫描 PDF 也可能获得 fullText 状态 | 按实际页面文本覆盖判定，增加 0/1/2 可读页反例。extract_paper.py，第 1285 行 |
| AI-032 | 首次安装技能后遗留临时文件 | 产生无用副本和后续维护噪声 | 首次写入后清理 temporary。improvement/index.js，第 1285 行 |
| AI-033 | 首次直接复用 OVITO 相机与视域设置 | 原子截面朝向与构图不符合本算例展示要求 | 独立相机适配器显式设 +x 向右、+z 向上及视域。第 850 行，最终图已检查 |
| AI-034 | Matplotlib 双面板布局与色标上限处理不足 | 图间可比性和最大位移颜色展示不足 | 采用 constrained 布局、共用色标并按所选原子最大位移定上限。第 818 行 |

## 操作与记录错误

| 编号 | 错误 | 影响及处置 | 证据 |
|---|---|---|---|
| AI-035 | 未先核对 Git 支持的 SSL backend，指定 openssl | 克隆尝试报 Unsupported SSL backend。随后按 Windows 支持的 schannel 路径继续 | 本聊天工具记录第 108 行 |
| AI-036 | 多次按推测路径读尚未存在或名称不符的文件 | 产生缺文件错误，增加无效调查。例如 workflow.json/tooling.json 实际内容在 manifest.json 中 | 第 164、463、479、530、1237 行 |
| AI-037 | 第一次导出快照时测试仍在写日志 | 初次快照中的日志可能未完整写入 | 随后重新导出 delivery-final，并核对完整 28 项日志。前轮导出调用及最终 manifest |
| AI-038 | 中间测试使用固定文件名覆盖 | 一部分历史失败细节需要从会话工具记录恢复 | test-results-first.log 保留最初 6 个失败，后续临时失败见 process-evidence 和聊天记录 |
| AI-039 | 本次文献工具调用使用了错误的参数字段 | 出现一次 Error parsing function call，随后按返回 schema 改正 | 当前聊天工具结果。已使用 pattern 字段重试成功 |
| AI-040 | 本次对照回执的 SHA 字段未明确换行规范 | 两个回执摘要来自 LF 规范化文本，不能直接当作 Windows 文件原始字节摘要 | 已另存实际 byteSha256 和摘要说明。[输入完整性](../../../simulation/audits/2026-11-17/control-input-integrity.json) |

## 环境与既有基线事件

以下事件也发生过，记录其实际来源有助于定位问题。

- GitHub 本地凭据/代理异常、443 连接失败及一次 API 404，之后成功取得指定仓库与固定提交。
- npm 下载出现 ECONNRESET，后续安装成功并生成依赖锁。
- Node 输出 SQLite ExperimentalWarning，三个原仿真作业的 LAMMPS 日志均无 WARNING。
- 原 Red Maple 基线存在时间预算与发送后 unknown 语义的测试差异。保留未知外部结果并在对账后恢复的语义已经过回归，当前为 76 通过、3 项外部环境依赖跳过。
- 清理/移动命令曾被自动审批规则拒绝，动作未执行。后续使用新的导出目录完成交付。
- 本次部分文献入口返回 403 或浏览器验证页，已取得的摘要和书目保持相应证据范围。

## 对已有结果的影响

原始主算例的 10 个采集输出与归档摘要全部一致。本次从 21 帧轨迹独立重算磨粒反力，与 CSV 的最大分量误差约 0.00328 nN。已有力值和原子位移的计算可复核。

AI-001 涉及声明和实际执行质量的一致性，影响量需专门敏感性计算。AI-002 已在隔离状态机复现，当前三个已完成原算例的日志、轨迹和图像核验记录仍可独立检查。全框架回归 28 通过只覆盖相应测试，当前新反例显示仍有测试覆盖缺口。
