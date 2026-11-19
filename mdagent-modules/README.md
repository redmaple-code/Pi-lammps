# MDagent 四个独立功能模块

版本 0.1.1，2026-11-17。此交付直接拆分原 MDagent 交接包，与 Pi 或任何特定 agent 工程无关。四个功能包保留原 Python 源码、测试、种子、各自的历史设计材料；公共代码归入共享底座。每包提供接口规范、来源清单、完整性清单与原许可。

| 模块 | 功能 | 组合依赖 | 接口规范 |
|---|---|---|---|
| knowledge | UKB 知识库、审核与检索 | shared；证据快照等跨系统功能还需 evidence | [知识接口](packages/0.1.1/knowledge/INTERFACE.md)、[MCP Schema](packages/0.1.1/knowledge/MCP-TOOLS.json) |
| evidence | 证据 v3、状态、审阅与许可链 | shared + knowledge + gate | [证据接口](packages/0.1.1/evidence/INTERFACE.md)、[MCP Schema](packages/0.1.1/evidence/MCP-TOOLS.json) |
| gate | 前置方案门、阶段门、审阅及度量 | shared + knowledge | [门禁接口](packages/0.1.1/gate/INTERFACE.md)、[MCP Schema](packages/0.1.1/gate/MCP-TOOLS.json) |
| consistency | 原物理一致性检查、报告与页面 | shared；UKB 知识查询可选 knowledge | [一致性接口](packages/0.1.1/consistency/INTERFACE.md) |
| shared | 路径、模型调用兼容层、公共模板等 | 无 | [共享接口](packages/0.1.1/shared/INTERFACE.md) |

包之间是**显式依赖**，不是已消除全部耦合：例如 gate 的 surface 实际调用知识适配器，evidence 的知识快照还引用门禁经验数据。`knowledge` 不强制依赖 evidence，但其旧兼容层中涉及证据快照/发布的操作必须另外组合 evidence。证据 v3 和旧阶段状态机不能同时对同一任务目录写入。

## 按需组合

需要 Python 3.11；本轮 Windows Python 3.11.4 已验证。前置门的 `physics_gate` 需要 numpy（本机 1.25.0），dsh 插件测试需要 Node（本机 22.19.0）。

在本目录执行（`<新目录>` 必须不存在，建议使用不受云同步的本机临时/部署路径）：

```powershell
# 使用四个功能中的一个；工具会自动带上必要依赖
python tools/split_handover.py compose --packages packages/0.1.1 --out <新目录> knowledge
python tools/split_handover.py compose --packages packages/0.1.1 --out <另一个新目录> evidence
python tools/split_handover.py compose --packages packages/0.1.1 --out <第三个新目录> gate
python tools/split_handover.py compose --packages packages/0.1.1 --out <第四个新目录> consistency

# 一次组合多个模块
python tools/split_handover.py compose --packages packages/0.1.1 --out <新目录> evidence consistency

# 校验全部交付字节的 SHA-256 清单
python tools/package_integrity.py check packages/0.1.1
```

组合器按原 `src/` 布局还原已选模块，验证各源码哈希、自动选择依赖，拒绝已有目录、冲突和危险相对路径，并输出 assembly.json。清单哈希只检查字节一致性，不是签名，无法阻止清单与文件同时被替换。`split` 是从本工作区保存的原包重建源分配的开发工具；在单独交付包中，无原始参考快照时只使用 `compose`。

运行时数据和密钥不随包提供。配置 `MDSIM_DATA_ROOT` 指向本机受控目录；知识系统如使用自定义根，`MDAGENT_UKB_ROOT` 与 `REVIEW_UKB_ROOT` 应指向同一目录。模块不提供宿主认证：接入 agent 前，宿主必须实现真实身份、权限/资源隔离、可信人工决定入口、秘密注入和审计保护。

## 接口与审计边界

每包 `INTERFACE.md` 列出原生函数、操作、数据/错误与依赖；三个 MCP 工具清单从原代码生成，不能代替服务端行为校验。知识 MCP 是独立服务；证据 MCP 使用原 `md_mcp` 聚合入口，物理一致性提供 Python/CLI；不同 agent 可通过 MCP 或自行创建薄适配，不需要 Pi。

保留 UKB 审核审计、v3 任务与工件链和原门禁记录，但**不能宣称原门禁审计已强制可靠**：其方案保存和审计存在已确认缺陷。不可因拆包完成就将 `confirm_quote` 当可信人工许可，或将一致性 PASS 视为物理科学结论。具体约束、已知缺陷和未验证功能见 [审计与验证](AUDIT-AND-VERIFICATION.md)。

## 来源与变更

原交接 ZIP SHA-256：`e7fbae3489c45985cace5cbe9dc03c3ffb4837799828889227f27b57531832ec`。包按文件归属拆分，共 300 个源文件；清单记录每个文件的来源、原哈希与当前哈希。0.1.1 只修改一致性模块两处：空/未知 verdict 不再误报 PASS；CLI 的 PASS 退出码改为 0。其余原缺陷仍保留，不以拆包冒充修复。

原文件目录下的 `source-docs/` 保留对应历史设计；共享包包含原总入口和打包说明。根目录内存有临时复测摘要和工具，测试不会访问生产库或真实凭据。原代码为 PolyForm Noncommercial 1.0.0，每包附原 LICENSE/NOTICE，实际使用及分发须符合原许可。
