# Agent 可插拔模块包

版本 0.1.0，协议 1.0.0。知识、证据、审查门禁、静态一致性检查各有一份独立 Node.js 安装包，也可使用包含四者的组合包。模块通过注入的后端工作，不依赖特定 agent 的 SDK；公共宿主提供权限与审计钩子。`adapters/simulation.js` 是本工作区的可选适配器，引用上一级 simulation 源码，不属于独立分发导出。

四个独立包位于 `dist/v0.1.0/{knowledge,evidence,gate,consistency}`，可分别通过 `npm install <绝对包目录>` 安装；每包自带与协议对应的 core，允许任意组合。`build-bundles.js <新输出目录>` 可重建，目标必须不存在，不覆盖先前交付。组合包仍可作为整体安装。知识模块目前对接 simulation 的 KnowledgeService，尚未对接原交接包 UKB；门禁模块对接 simulation GateService，未直接暴露原 `md_proj_mcp` 或 `sim_exec`。Python 跨进程协议、MCP 封装、真实其他 agent 宿主、原包的其余能力与生产验收仍待完成。

## 快速接入

使用 Node 22.19.0（当前验证版本），将本目录作为整体依赖安装或复制。宿主将身份从认证通道获得，按资源范围授权，并注入已经初始化的后端和持久审计对象：

```js
import { ModuleHost } from '@redmaple/agent-modules/core';
import { SqliteAudit } from '@redmaple/agent-modules/audit-sqlite';
import { createKnowledgeModule } from '@redmaple/agent-modules/knowledge';

const audit = new SqliteAudit('/local-controlled-state/module-audit.sqlite');
const host = new ModuleHost({
  audit,
  // Replace with the host's authentication, resource and capability policy.
  authorize: (actor, capability, { moduleId, operation, input }) =>
    hostPolicyAllows(actor, capability, moduleId, operation, input)
});
host.register(createKnowledgeModule({ backend: hostKnowledgeService }));
// actor is the authenticated host context, never a field supplied by the model.
const result = await host.call('knowledge', 'search', { query: 'boundary conditions' }, {
  actor: authenticatedActor, requestId: hostGeneratedRequestId
});
// Later: host.unregister('knowledge'); audit.close();
```

示例中的 `hostPolicyAllows`、`hostKnowledgeService`、`authenticatedActor`、`hostGeneratedRequestId` 为宿主提供，不是内置全局变量。Windows 路径使用本机受控目录；目录需由宿主事先创建并设置访问权限。示例不设置宽松默认策略。

使用独立知识包时，改为从 `@redmaple/agent-knowledge` 导入 `createKnowledgeModule`，从 `@redmaple/agent-knowledge/core` 和 `@redmaple/agent-knowledge/audit-sqlite` 导入宿主及审计。其余三个包分别是 `@redmaple/agent-evidence`、`@redmaple/agent-gate`、`@redmaple/agent-consistency`，对应导出 `createEvidenceModule`、`createGateModule`、`createConsistencyModule`。宿主从任意一个独立包导入 `ModuleHost` 后可以注册所有四种模块，版本必须一致。相同进程中只需一个宿主和一个审计实例。

在当前工程中，可使用 `adapters/simulation.js` 的 `installSimulationModules`，分别传入已经初始化的 `KnowledgeService`、`EvidenceService`、`GateService` 和可信 `resolveEvidenceSource(sourceId, actor)`；未注入的后端不会注册。LAMMPS 检查器默认注册，传 `lammps:false` 禁用。

## 操作契约

所有操作为异步函数。`host.call(moduleId, operation, input, {actor, requestId})`；`actor` 必须有 `actorId`、`actorInstanceId`、`kind=human|agent|service`，其真实性由宿主保证。`requestId` 是 1–128 个字母、数字、点、下划线或横线。宿主授权回调接收能力及输入，必须同时检查租户、资源与操作范围；模块库没有认证服务器、资源隔离或 sandbox。

| 子模块 / 操作 | 输入 | 输出 | 副作用 / 最低能力 |
|---|---|---|---|
| knowledge.search | `query` 非空、最多 4096 字符；`options.domain?`, `options.limit?`（1–50） | 后端搜索页；状态与来源由后端返回 | read / `knowledge.search` |
| knowledge.read | `sourceId`, `scope?` | 后端实读回执 | write：记录实读 / `knowledge.read` |
| evidence.read | `sourceId`, `scope?` | 带实际读范围与源身份的后端回执 | write：记录实读 / `evidence.read` |
| evidence.assess | 非空 `readRefs[]`, `policy?` | 后端证据评估 | read / `evidence.assess` |
| gate.submit | `caseId`, `stageId`, 非空 `subjectRefs[]`/`checks[]`，以及后端需要的 `producerInstanceId`、`evidenceRefs` 等 | 后端审查回执；不直接推进阶段 | write：保存审查 / `review.submit` |
| consistency.inspect | 非空 `text`（最多 2,000,000 字符），`options?` | `checkSetVersion, requiredCheckIds, checks, missingCheckIds, verdict, coverageComplete, limitations` | read / `consistency.inspect` |

知识后端提供 `search(query, options)` 与 `read(sourceId, scope, actor)`；证据后端提供 `read(source, scope, actor)` 与 `assess(readRefs, policy, actorInstanceId)`；门禁后端提供 `submit(input, actor)`。完整字段和错误语义以注入后端契约为准。`resolveEvidenceSource` 由宿主提供；调用方不能通过请求指定任意本地文件路径。生产宿主仍必须按 `input` 检查资源归属；证据读取回执和 `knowledge.read` 的可见性同样受宿主策略约束。

一致性检查器提供 `inspect(text, options)`，返回 `{checkId, applicability: true|false|'unknown', result:'passed'|'failed'|'insufficient'|'notApplicable', ...}[]`。模块冻结非空必需检查 ID 列表，拒绝重复/未知值，缺项或 unknown 得 REVIEW，实际失败得 FAIL；只有覆盖完整且无失败/不充分项才 PASS。单独的 PASS 不是科学验证或执行授权。当前 LAMMPS 检查集是 simulation 的 MB-001–009 与 OUTPUT-CLOSURE，不是交接包的 intent↔in.case 全量检查器。

可用功能见 `host.list()`；未注册操作返回 `CAPABILITY_UNAVAILABLE`。只允许加载可信模块及可信后端；普通 Node 进程内模块可访问进程资源，注册接口本身不构成隔离。协议号需完全匹配；当前没有自动版本迁移。

## 失败和审计

`ModuleError` 包含 `code`, `retryable=false`, `sideEffectState=none|unknown`。宿主应识别 `CONFIG_INVALID`, `IDENTITY_REQUIRED`, `SCHEMA_INVALID`, `CAPABILITY_UNAVAILABLE`, `PERMISSION_DENIED`, `EVIDENCE_INSUFFICIENT`, `REFERENCE_MISSING`, `AUDIT_UNAVAILABLE`, `MODULE_FAILURE` 和后端已定义的大写错误码。异常消息是通用文本，详细业务排查依靠后端受控记录。

每次有效、已注册调用先写 started，再授权与调用，最后写 completed 或 failed。审计记录仅含调用 ID、模块/操作/版本、effect、时间、状态、请求 ID/actor instance/输入与输出的摘要，不含请求正文、文件路径、查询或凭据。摘要可能被猜测或关联，审计数据库仍属于受控数据；绝不能将个人 L1 明文或任何 L3 密值放入请求、日志或同步目录。审计路径应由部署者放在本地非同步目录。

每次 append 检查链完整性，SQLite `BEGIN IMMEDIATE` 与 FULL 同步用于序列化持久追加。链可检测链内篡改，但无法独立发现整个数据库或尾部被替换/删除；正式部署需另行实现受保护的外部链头锚定、备份、访问控制与保留策略。审计数据不与业务事务原子提交：started 后异常中断、外部副作用后最终记录失败均须由宿主对账。写入最终审计失败返回 `AUDIT_UNAVAILABLE/unknown`；不得自动重试。

`requestId` 当前仅用于审计关联，**不是幂等键**。本包不为任意后端保证幂等、取消、超时或 exactly-once；不得对不明状态的写入直接重放。授权失败也会有 started/failed 记录；无效身份、未注册操作及审计开始前的输入校验不会留下本模块审计记录，需要外围网关补充安全日志。

## 验证和限制

在 `simulation` 目录执行 `node --test --test-concurrency=1 tests/agent-modules.test.js`。测试使用真实 simulation SQLite 状态、证据/知识/门禁服务和 LAMMPS 静态检查器，外部模型、UKB、求解器、调度器与不同 agent 产品宿主均未实测。真实服务适配、权限产品语义、所有 HR 问题修复与生产数据恢复不因这些测试通过而完成。

原 MDagent 交接包采用 PolyForm Noncommercial，本分发包没有复制其源码；引入原包源代码或数据前，须保留原许可并核对使用授权。本包未重新授权原资产。
