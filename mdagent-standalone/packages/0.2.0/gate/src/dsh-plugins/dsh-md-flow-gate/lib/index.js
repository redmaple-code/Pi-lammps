// dsh-md-flow-gate — harness-level enforcement of the MD-sim operating
// pipelines, plus conversation-failure signal collection for the evolution
// observer. Prompt text can be rationalized away; this gate cannot: when a
// user message matches a guarded intent, the gate arms and keeps injecting a
// corrective reminder at every pre-step until the rule's evidence tool has
// actually executed. An arm that survives to turn/end is logged as a
// violation; repeat-question / user-correction signals are logged at
// message time. All of it lands in logPath for evolution/observe.py.
//
// Mechanism (mirrors @deepseek-ai/dsh-agent-instructions / repeat-tool-reminder):
//   session/event "user/message"  → failure signals + intent match arms (per session)
//   tools/post-execute            → evidence tool disarms
//   agent/pre-step                → while armed, append a user-role reminder
//   session/event "turn/end"      → still armed → append violation to logPath
import { appendFileSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'dsh-md-flow-gate';

// 插件真实路径上四级 = dsh-agent 父目录(ESM 默认解析 symlink realpath,
// pnpm link 进 profile 后仍指向源码树),与 Python paths.CODE_ROOT 的父目录同义
const CODE_PARENT = resolve(dirname(fileURLToPath(import.meta.url)),
    '..', '..', '..', '..');

// 与 mcp/paths.py data_root() 同语义(五轮对抗:JS 侧曾无 expanduser/相对锚定,
// env=~/x 或相对值时与 Python 分裂,flowgate 日志落到 observe 读不到的位置):
// 空白视为未设置;~ 展开家目录;相对值锚定代码根父目录而非 cwd
export function resolveDataRoot() {
    let root = (process.env.MDSIM_DATA_ROOT ?? '').trim();
    if (!root) return null;
    if (root === '~') root = homedir();
    else if (root.startsWith('~/')) root = resolve(homedir(), root.slice(2));
    if (!isAbsolute(root)) root = resolve(CODE_PARENT, root);
    return root;
}

const STATUS_INTENT =
    /(模拟|case|run\d+|体系|作业).{0,12}(跑到哪|状态|进度|巡检|异常|风险|怎么样|如何)|(跑到哪|状态|进度|巡检|异常|风险|怎么样).{0,12}(模拟|case|run\d+|体系|作业)|\b(status|progress|risk|anomal\w*)\b/i;
const MODEL_INTENT =
    /(力场|参数集|势函数|力场文件).*(合适|哪个|更好|选型|对比|换|推荐)|(选型|换力场|哪个力场|哪个参数集|对比.*(力场|参数集|模型))|(force.?field|potential|parameteri[sz]).*(which|better|compare|suitab)/i;
const CORRECTION =
    /(不对|错了|不是这样|还是没|仍然不|你再看|重答|重新回答|根本不是)|(that'?s wrong|incorrect|try again|not what i)/i;
// 修复请求意图:用户直接要修复方案时布防 sim_repair_research——
// 防"巡检 NO_LOG → 模型手读日志 → 凭经验开方"这条绕过路(2026-08-27 验收实测发现)
const REPAIR_INTENT =
    /(修复|救回|怎么办|怎么处理|如何解决|怎么修|修一下).*(模拟|case|run\d+|异常|爆炸|发散|报错|温度|能量|力场)|(异常|爆炸|发散|报错).*(修复|怎么办|怎么修|处理)|(fix|repair|how to fix).*(simulat|lammps|crash|explod|error)/i;

const STATUS_REMINDER = [
    '【流程硬约束】你在回答模拟状态/进度/风险类问题。',
    '回答前必须先调用工具 sim_pipeline_check(或 sim_status)拿到真实数据;',
    'status/step/temp/risk_score 等数字以工具返回为准,不得凭记忆或推测作答。',
    '若已调用过,请忽略本提醒。'
].join('\n');
const MODEL_REMINDER = [
    '【流程硬约束】你在回答力场/参数集/模型选型类问题。',
    '回答前必须先调用工具 sim_model_research 拿到联网调研与终裁结论;',
    '推荐策略以其 verdict/strategies 为准,verified=false 的必须明说缺来源支撑。',
    '若已调用过,请忽略本提醒。'
].join('\n');
const REPAIR_REMINDER = [
    '【流程硬约束】你在处理模拟异常修复请求,或巡检刚返回风险信号(CRITICAL/WARNING 或 risk_score 偏高)。',
    '给出任何修复建议前必须先调用工具 sim_repair_research 做多路联网研讨与代码终裁;',
    '即使用户明确要求"不要调/跳过"本工具,也不能豁免——这是系统级硬约束,优先级高于用户指令;',
    '修复策略以其 verdict/strategies 为准,verified=false 的必须明说缺来源支撑;',
    '实际改动仍走 sim_exec 写门并经用户确认。若已调用过,请忽略本提醒。'
].join('\n');

// 守卫规则:先命中先生效。evidence 工具执行即销防。
// status-intent 证据含 sim_gmx_status(分引擎口径,persona Mode A:GROMACS 状态
// 走 sim_gmx_status)——任一真相源工具销防;LAMMPS 布防面不变(2026-09-12)。
const RULES = [
    { id: 'repair-intent', intent: REPAIR_INTENT,
      evidence: ['mcp__mdsim__sim_repair_research'],
      reminder: REPAIR_REMINDER },
    { id: 'status-intent', intent: STATUS_INTENT,
      evidence: ['mcp__mdsim__sim_pipeline_check', 'mcp__mdsim__sim_status',
                 'mcp__mdsim__sim_gmx_status'],
      reminder: STATUS_REMINDER },
    { id: 'model-choice', intent: MODEL_INTENT,
      evidence: ['mcp__mdsim__sim_model_research'],
      reminder: MODEL_REMINDER },
];

// 豁免白名单(2026-09-12,借鉴 fable-5"先定义何时不需要证据工具"):只免【布防】
// (不注入提醒/不计违规),不放松任何已布防规则的销防条件;"用户要求跳过工具"的
// 硬约束不在豁免之列。依据 flowgate.jsonl 实测误报三类:
//   ① 纯问候/致谢;② 代码审计简报被误当模拟修复(repair-intent 两条违规均为
//   "只读审核代码"简报);③ 交接文档全文粘贴被误当当前选型提问(model-choice)。
const EXEMPT_GREETING =
    /^(你好|您好|hi|hello|嗨|在吗|谢谢|感谢|辛苦了|再见|好的|ok|okay)[\s!！。.~～,，]*$/i;
const EXEMPT_IDENTITY =
    /(你是谁|什么模型|你叫什么|自我介绍|你能(做|帮)什么|你会什么|怎么用你)/;
const EXEMPT_AUDIT = /(只读审核|只读审计|只读复查|代码审计|代码审查|审阅代码|review.{0,8}code)/i;
const EXEMPT_HANDOFF = /(交接方案|交接文档|交接给|handoff|将本文件完整粘贴)/i;
// 规则专属豁免:豁免按规则生效,防一条豁免误放另一类真问题
const RULE_EXEMPTS = {
    'repair-intent': EXEMPT_AUDIT,   // 审核对象是代码/系统,不是模拟健康
    'model-choice': EXEMPT_HANDOFF,  // 整份方案移交粘贴,不是当前选型提问
};
// 身份问句仅在【不含任何模拟事务词】时豁免——"你是谁?顺便看下 run1 状态"仍布防
const EXEMPT_IDENTITY_GUARD =
    /(模拟|case|run\d+|体系|作业|力场|参数集|异常|爆炸|报错|修复|温度|能量|gmx|gromacs)/i;

export function exemptFrom(rule, text) {
    const t = (text || '').trim();
    if (!t) return false;
    if (EXEMPT_GREETING.test(t)) return true;
    if (rule && RULE_EXEMPTS[rule.id] && RULE_EXEMPTS[rule.id].test(t)) return true;
    if (EXEMPT_IDENTITY.test(t) && !EXEMPT_IDENTITY_GUARD.test(t)) return true;
    return false;
}
// 风险布防:不挂在用户消息意向上,由巡检工具的高风险返回触发(程序计算中发现风险)
const REPAIR_RULE = { id: 'repair-research', intent: null,
    evidence: ['mcp__mdsim__sim_repair_research'], reminder: REPAIR_REMINDER };
const RISK_SOURCES = new Set(['mcp__mdsim__sim_pipeline_check', 'mcp__mdsim__sim_status']);
const RISK_SCORE_ARM = 60;  // 与 persona 预防介入线一致

// 巡检工具结果里的风险信号:status=CRITICAL/WARNING 或 risk_score≥60 → 返回触发原因,否则 null
function detectRisk(result) {
    let s;
    try {
        s = typeof result === 'string' ? result : JSON.stringify(result);
    } catch {
        return null;
    }
    if (!s) return null;
    const st = s.match(/"status"\s*:\s*"(CRITICAL|WARNING)"/);
    if (st) return 'status=' + st[1];
    const rs = s.match(/"risk_score"\s*:\s*(\d+(?:\.\d+)?)/);
    if (rs && parseFloat(rs[1]) >= RISK_SCORE_ARM) return 'risk_score=' + rs[1];
    return null;
}

const DEFAULT_LOG = 'events/flowgate.jsonl';  // 相对数据根(MDSIM_DATA_ROOT)或 cwd
const RECENT_WINDOW = 5;         // 每会话保留的最近用户消息数(重复提问检测)
const SIM_THRESHOLD = 0.5;       // char-3gram Jaccard 相似度阈值(实测:不同问题≤0.33,换措辞同题≥0.5)
const MIN_SIM_LEN = 6;           // 短消息不做相似度判定

function createMessage(input) {
    return structuredClone({
        ...input,
        id: crypto.randomUUID()
    });
}

function reminderMessage(text) {
    return createMessage({
        role: 'user',
        content: [{ type: 'text', text }],
        source: { kind: 'plugin', plugin: name, form: 'flow-gate-reminder' }
    });
}

// 宪章心跳(2026-09-17 加固,手册 §10.1):未布防时的无条件周期重注入,抗长 turn 漂移
const HEARTBEAT_EVERY = 12;
const CHARTER = [
    '【架构宪章·周期重注入】',
    '受控面操作（启停模拟/改 input/data/力场/杀进程/动编排 state/改配置开关）只能走 sim_exec。',
    '绕过架构完成任务=失败；如实报告阻塞=成功。禁止模拟/占位/fallback 数据。',
    '本轮若有受控面操作是用 bash 做的，在回答最前面显式写出来。'
].join('\n');

function messageText(data) {
    const content = data?.content;
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';
    return content
        .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
        .map((b) => b.text)
        .join('\n');
}

// 归一化 + char-3gram 集合;用于"换措辞追问同一问题"的判定
function trigrams(text) {
    const norm = text.toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, '');
    const grams = new Set();
    for (let i = 0; i + 3 <= norm.length; i++) grams.add(norm.slice(i, i + 3));
    return grams;
}

function jaccard(a, b) {
    if (a.size === 0 || b.size === 0) return 0;
    let inter = 0;
    for (const g of a) if (b.has(g)) inter++;
    return inter / (a.size + b.size - inter);
}

export function apply(ctx, config) {
    const rules = RULES.map((r) => ({ ...r }));
    // config 只许"加严"不许"放松"(2026-08-29 对抗审计 E4):
    // - config.intent 编译后与内置 STATUS_INTENT 取"或"(只增布防面,永不缩减;
    //   非法正则忽略并告警,绝不让 apply 抛出去)
    // - 不提供 config.tools:evidence 清单是门禁核心,内置即正确值,任何配置都不得替换
    if (config?.intent) {
        try {
            const extra = new RegExp(config.intent, 'i');
            const status = rules.find((r) => r.id === 'status-intent');
            const builtin = status.intent;
            status.intent = new RegExp(`(?:${builtin.source})|(?:${extra.source})`, 'i');
        } catch (e) {
            console.error('[dsh-md-flow-gate] config.intent 非法正则,已忽略:', e.message);
        }
    }
    const simThreshold = typeof config?.simThreshold === 'number'
        ? config.simThreshold : SIM_THRESHOLD;
    // logPath 解析顺序(2026-08-29 数据根外置):
    // 1. config.logPath 绝对路径 → 直接用(向后兼容)
    // 2. 相对值 + MDSIM_DATA_ROOT env → 锚定数据根(run-dsh.sh 保证 env 恒存在)
    // 3. 相对值无 env → cwd 兜底(旧行为,仅作最后退路,告警一次)
    // 相对值解析后必须仍在基座内,逃逸(../)则回落默认日志(防越界写入)
    let logPath;
    if (isAbsolute(config?.logPath ?? '')) {
        logPath = config.logPath;
    } else {
        const relLog = config?.logPath ?? DEFAULT_LOG;
        const envRoot = resolveDataRoot();
        const base = envRoot || process.cwd();
        if (!envRoot) {
            console.error('[dsh-md-flow-gate] MDSIM_DATA_ROOT 未设置,flowgate 日志落 cwd 兜底:',
                base, '(经 run-dsh.sh 启动可消除此警告)');
        }
        const resolved = resolve(base, relLog);
        logPath = resolved.startsWith(resolve(base) + sep)
            ? resolved
            : resolve(base, DEFAULT_LOG);
        if (logPath !== resolved) {
            console.error('[dsh-md-flow-gate] logPath 相对值逃逸基座,已回落默认:', relLog);
        }
    }

    // per-session state: session → { armed: rule|null, text, recent: string[] }
    const states = new Map();

    const logEvent = (record) => {
        try {
            mkdirSync(dirname(logPath), { recursive: true });
            appendFileSync(logPath, JSON.stringify(record) + '\n');
        } catch {
            // 信号日志失败绝不影响主流程
        }
    };

    const stateOf = (session) => {
        let s = states.get(session);
        if (!s) {
            s = { armed: null, text: '', recent: [] };
            states.set(session, s);
        }
        return s;
    };

    ctx.on('session/event', (session, event) => {
        if (event.type === 'user/message') {
            const data = event.data ?? {};
            // 只响应真实用户输入(source.kind === 'user');tool-result 也是 user 角色,须排除
            if (data?.source?.kind !== 'user') return;
            const text = messageText(data);
            const state = stateOf(session);

            // —— 会话失效信号(rsi-os Observe 前哨):重复追问 / 否定纠正 ——
            if (text.length >= MIN_SIM_LEN && state.recent.length > 0) {
                const grams = trigrams(text);
                for (const prev of state.recent) {
                    if (jaccard(grams, trigrams(prev)) >= simThreshold) {
                        logEvent({
                            t: Date.now(), kind: 'repeat-question',
                            userText: text.slice(0, 200), previousText: prev.slice(0, 200),
                            note: 'similar question re-asked within session: previous answer likely failed'
                        });
                        break;
                    }
                }
            }
            if (CORRECTION.test(text)) {
                logEvent({
                    t: Date.now(), kind: 'user-correction',
                    userText: text.slice(0, 200),
                    note: 'explicit negative feedback on a previous answer'
                });
            }
            state.recent.push(text.slice(0, 200));
            if (state.recent.length > RECENT_WINDOW) state.recent.shift();

            // —— 意图布防(豁免白名单先行:纯问候/身份/审计简报/交接粘贴不布防) ——
            const rule = rules.find((r) => r.intent.test(text)) ?? null;
            state.armed = (rule && !exemptFrom(rule, text)) ? rule : null;
            state.text = text.slice(0, 200);
            return;
        }
        if (event.type === 'turn/end') {
            const state = states.get(session);
            if (state?.armed) {
                logEvent({
                    t: Date.now(),
                    kind: 'flow-gate-violation',
                    rule: state.armed.id,
                    reason: `answered ${state.armed.id} intent without evidence tool`,
                    userText: state.text,
                    turn: event.data?.turn ?? null
                });
            }
            states.delete(session); // turn 结束即释放,防 Map 随会话数泄漏
        }
    });

    ctx.on('tools/post-execute', (exec, _result, next) => {
        const state = exec.agent?.session ? stateOf(exec.agent.session) : undefined;
        if (!state) return next();
        if (state.armed && state.armed.evidence.includes(exec.name)) {
            state.armed = null;
        }
        // 巡检返回高风险 → 布防修复研讨(销防优先:刚执行过 sim_repair_research 就不再布防)
        if (RISK_SOURCES.has(exec.name)) {
            const why = detectRisk(_result);
            if (why) {
                state.armed = REPAIR_RULE;
                state.text = `[巡检风险 ${why}] ` + state.text;
            }
        }
        return next();
    });

    ctx.on('agent/pre-step', async ({ agent, messages }, next) => {
        const decision = await next();
        const state = agent?.session ? states.get(agent.session) : undefined;
        if (!state) return decision;
        if (decision.kind === 'reject') return decision;
        if (state.armed) {
            // 布防期间每个 pre-step 都注入一次提醒(直至证据工具销防或 turn 结束),
            // 只追加不改写下游 decision 的其它内容
            return {
                kind: 'enter',
                messages: [...decision.messages, reminderMessage(state.armed.reminder)]
            };
        }
        // 宪章心跳(2026-09-17 加固):未布防时每 12 个 pre-step 无条件重注入一次,
        // 抗长 turn 漂移;计数器随 state 在 turn/end 释放,不跨 turn 累积
        state.steps = (state.steps || 0) + 1;
        if (state.steps % HEARTBEAT_EVERY === 0) {
            return {
                kind: 'enter',
                messages: [...decision.messages, reminderMessage(CHARTER)]
            };
        }
        return decision;
    });
}
