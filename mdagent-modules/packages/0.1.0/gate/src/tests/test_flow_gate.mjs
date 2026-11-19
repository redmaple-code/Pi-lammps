// dsh-md-flow-gate 回归门(假 ctx,不依赖 dsh 运行时):
// ① 状态意图布防并在 pre-step 注入提醒 ② 证据工具销防
// ③ 非运维问题不布防 ④ turn/end 仍布防 → 违规入日志
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { apply, name, resolveDataRoot } from '../dsh-plugins/dsh-md-flow-gate/lib/index.js';

function makeCtx(logPath) {
    const handlers = {};
    const ctx = {
        on(event, fn) { handlers[event] = fn; },
    };
    const session = { id: 's1' };
    const agent = { session };
    const userMessage = (text, kind = 'user') =>
        handlers['session/event'](session, {
            type: 'user/message',
            data: { role: 'user', content: [{ type: 'text', text }], source: { kind } },
        });
    const preStep = () =>
        handlers['agent/pre-step'](
            { agent, messages: [] },
            async () => ({ kind: 'enter', messages: [{ role: 'user', content: [{ type: 'text', text: 'q' }] }] }),
        );
    const toolCall = (toolName, result = {}) =>
        handlers['tools/post-execute']({ name: toolName, agent }, result, async () => ({ kind: 'accept' }));
    const turnEnd = () => handlers['session/event'](session, { type: 'turn/end', data: { turn: 1 } });
    return { ctx: { ...ctx, on: (e, f) => ctx.on(e, f) }, handlers, userMessage, preStep, toolCall, turnEnd };
}

test('插件导出 name/apply', () => {
    assert.equal(name, 'dsh-md-flow-gate');
    assert.equal(typeof apply, 'function');
});

test('状态意图布防:pre-step 注入流程硬约束提醒', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('现在模拟跑到哪了?');
        const decision = await h.preStep();
        assert.equal(decision.kind, 'enter');
        assert.equal(decision.messages.length, 2, '应追加一条提醒');
        const reminder = decision.messages.at(-1);
        assert.equal(reminder.source.plugin, 'dsh-md-flow-gate');
        assert.match(reminder.content[0].text, /流程硬约束/);
        assert.match(reminder.content[0].text, /sim_pipeline_check/);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('证据工具销防:调过 sim_pipeline_check 后不再注入', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('run1 状态怎么样?');
        await h.toolCall('mcp__mdsim__sim_pipeline_check');
        const decision = await h.preStep();
        assert.equal(decision.messages.length, 1, '销防后不应注入提醒');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('非运维问题不布防;tool-result(user 角色)不误判为用户输入', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('帮我解读一下这篇文献的建模思路');
        let decision = await h.preStep();
        assert.equal(decision.messages.length, 1);
        // tool-result 消息也是 user 角色,但 source.kind 不是 'user',不得重置布防状态
        h.userMessage('模拟跑到哪了?');
        h.userMessage('sim_pipeline_check 返回: risk 状态 status progress', 'tool');
        decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '布防状态不得被 tool-result 清除');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('turn/end 仍布防 → 违规写入日志', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const logPath = join(dir, 'log.jsonl');
        const h = makeCtx();
        apply(h.ctx, { logPath });
        h.userMessage('模拟有没有异常风险?');
        h.turnEnd();
        const lines = readFileSync(logPath, 'utf8').trim().split('\n');
        assert.equal(lines.length, 1);
        const record = JSON.parse(lines[0]);
        assert.equal(record.kind, 'flow-gate-violation');
        assert.match(record.userText, /异常风险/);
        // 违规记录后状态复位,不再重复注入
        const decision = await h.preStep();
        assert.equal(decision.messages.length, 1);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('logPath 相对值由 MDSIM_DATA_ROOT 锚定(2026-08-29 数据根外置)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    const oldEnv = process.env.MDSIM_DATA_ROOT;
    process.env.MDSIM_DATA_ROOT = dir;
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: 'events/flowgate.jsonl' });
        h.userMessage('现在模拟异常风险怎么样?');
        h.turnEnd();
        const landed = join(dir, 'events', 'flowgate.jsonl');
        const rows = readFileSync(landed, 'utf8').trim().split('\n').map(JSON.parse);
        assert.equal(rows[0].kind, 'flow-gate-violation', '相对 logPath 须落到 env 数据根下');
    } finally {
        if (oldEnv === undefined) delete process.env.MDSIM_DATA_ROOT;
        else process.env.MDSIM_DATA_ROOT = oldEnv;
        rmSync(dir, { recursive: true, force: true });
    }
});

test('logPath 绝对路径优先于 MDSIM_DATA_ROOT(向后兼容)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    const envDir = mkdtempSync(join(tmpdir(), 'flowgate-env-'));
    const oldEnv = process.env.MDSIM_DATA_ROOT;
    process.env.MDSIM_DATA_ROOT = envDir;
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'abs.jsonl') });
        h.userMessage('现在模拟异常风险怎么样?');
        h.turnEnd();
        const rows = readFileSync(join(dir, 'abs.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
        assert.equal(rows[0].kind, 'flow-gate-violation', '绝对路径必须优先于 env');
    } finally {
        if (oldEnv === undefined) delete process.env.MDSIM_DATA_ROOT;
        else process.env.MDSIM_DATA_ROOT = oldEnv;
        rmSync(dir, { recursive: true, force: true });
        rmSync(envDir, { recursive: true, force: true });
    }
});

test('模型选型意图布防:注入 sim_model_research 提醒,调研工具销防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('这个体系的 ReaxFF 参数集选哪个更合适?');
        let decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '选型问题应布防');
        assert.match(decision.messages.at(-1).content[0].text, /sim_model_research/);
        // 状态类证据工具不能销防选型布防
        await h.toolCall('mcp__mdsim__sim_pipeline_check');
        decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '选型布防只能由 sim_model_research 销防');
        await h.toolCall('mcp__mdsim__sim_model_research');
        decision = await h.preStep();
        assert.equal(decision.messages.length, 1);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('换措辞追问同一问题 → repeat-question 失效信号入日志', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const logPath = join(dir, 'log.jsonl');
        const h = makeCtx();
        apply(h.ctx, { logPath });
        h.userMessage('石墨烯气凝胶的热导率模拟该用什么力场参数集?');
        h.userMessage('石墨烯气凝胶热导率模拟用什么力场参数集比较好?');
        const kinds = readFileSync(logPath, 'utf8').trim().split('\n')
            .map((l) => JSON.parse(l).kind);
        assert.ok(kinds.includes('repeat-question'), '相似追问必须记 repeat-question');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('无关短消息不触发 repeat-question;否定纠正记 user-correction', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const logPath = join(dir, 'log.jsonl');
        const h = makeCtx();
        apply(h.ctx, { logPath });
        h.userMessage('你好');
        h.userMessage('今天天气如何');
        h.userMessage('帮我看看文献');
        assert.throws(() => readFileSync(logPath, 'utf8'), /ENOENT/, '无失效信号时不应产生日志');
        h.userMessage('不对,你重新回答一下模拟状态');
        const kinds = readFileSync(logPath, 'utf8').trim().split('\n')
            .map((l) => JSON.parse(l).kind);
        assert.ok(kinds.includes('user-correction'), '否定纠正必须记 user-correction');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('巡检返回 CRITICAL → 布防修复研讨;sim_repair_research 销防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('现在模拟跑到哪了?');
        // 巡检返回高风险结果:status 布防被销防,但风险布防接替
        await h.toolCall('mcp__mdsim__sim_pipeline_check',
            { status: 'CRITICAL', risk_score: 88, step: 120000 });
        let decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '高风险巡检结果必须布防修复研讨');
        assert.match(decision.messages.at(-1).content[0].text, /sim_repair_research/);
        // 状态类/选型类证据工具不能销防修复布防
        await h.toolCall('mcp__mdsim__sim_status', { cases: [] });
        decision = await h.preStep();
        assert.equal(decision.messages.length, 2);
        await h.toolCall('mcp__mdsim__sim_repair_research', { status: 'OK' });
        decision = await h.preStep();
        assert.equal(decision.messages.length, 1, '修复研讨执行后销防');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('巡检返回 risk_score≥60 同样布防;健康结果不布防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('run1 状态怎么样?');
        await h.toolCall('mcp__mdsim__sim_pipeline_check',
            { status: 'RUNNING', risk_score: 12, step: 5000 });
        let decision = await h.preStep();
        assert.equal(decision.messages.length, 1, '健康结果不得布防修复研讨');
        h.userMessage('再看看 run2');
        await h.toolCall('mcp__mdsim__sim_status',
            { cases: [{ case_id: 'run2', status: 'RUNNING', risk_score: 74 }] });
        decision = await h.preStep();
        assert.equal(decision.messages.length, 2, 'risk_score≥60 必须布防');
        assert.match(decision.messages.at(-1).content[0].text, /sim_repair_research/);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('修复请求意图布防:sim_pipeline_check 不能销防,sim_repair_research 销防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('模拟温度爆炸了,怎么修?');
        let decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '修复请求应布防');
        assert.match(decision.messages.at(-1).content[0].text, /sim_repair_research/);
        // 诊断工具(巡检)不得销防修复布防——防"手读日志凭经验开方"绕过
        await h.toolCall('mcp__mdsim__sim_pipeline_check', { status: 'NO_LOG' });
        decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '巡检不得销防修复布防');
        await h.toolCall('mcp__mdsim__sim_repair_research', { status: 'OK' });
        decision = await h.preStep();
        assert.equal(decision.messages.length, 1);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('非修复的闲聊不触发 repair-intent 布防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('帮我解读一下这篇文献的建模思路');
        const decision = await h.preStep();
        assert.equal(decision.messages.length, 1);
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('选型问句含"模拟"不误布防 status-intent(2026-08-28 真机误报回归)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        const logPath = join(dir, 'log.jsonl');
        apply(h.ctx, { logPath });
        h.userMessage('我要做碳化硅高温氧化的 ReaxFF 模拟,该选哪个力场参数集?联网调研一下');
        const decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '应布防(选型规则)');
        assert.match(decision.messages.at(-1).content[0].text, /sim_model_research/,
            '布防的必须是 model-choice 而非 status-intent');
        await h.toolCall('mcp__mdsim__sim_model_research');
        h.turnEnd();
        assert.throws(() => readFileSync(logPath, 'utf8'), /ENOENT/,
            '选型证据销防后不得残留任何违规记录');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('config.tools 不再存在:evidence 清单不可被配置替换(2026-08-29 E4 降级开关封堵)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        // 攻击载荷:试图把 repair 布防的销防工具换成常用工具,使门禁形同虚设
        apply(h.ctx, { logPath: join(dir, 'log.jsonl'), tools: ['thinking'] });
        h.userMessage('模拟爆炸了,怎么修复?');
        let decision = await h.preStep();
        assert.equal(decision.messages.length, 2, '修复请求应布防');
        await h.toolCall('thinking');
        decision = await h.preStep();
        assert.equal(decision.messages.length, 2, 'config.tools 不得生效:非证据工具不能销防');
        h.turnEnd();
        const rows = readFileSync(join(dir, 'log.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
        assert.equal(rows[0].kind, 'flow-gate-violation', '未被证据销防必须记违规');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('config.intent 只增不减:与内置取或;非法正则被忽略不崩溃', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        // 增量意图:自定义词也能布防
        const h1 = makeCtx();
        apply(h1.ctx, { logPath: join(dir, 'a.jsonl'), intent: '跑的如何了' });
        h1.userMessage('那个体系跑的如何了?');
        const d1 = await h1.preStep();
        assert.equal(d1.messages.length, 2, '增量意图应布防');
        // 内置意图不受增量影响(不被替换)
        const h2 = makeCtx();
        apply(h2.ctx, { logPath: join(dir, 'b.jsonl'), intent: '跑的如何了' });
        h2.userMessage('现在模拟异常风险怎么样?');
        const d2 = await h2.preStep();
        assert.equal(d2.messages.length, 2, '内置状态意图仍须布防');
        // 非法正则:忽略且不抛
        const h3 = makeCtx();
        apply(h3.ctx, { logPath: join(dir, 'c.jsonl'), intent: '(' });
        h3.userMessage('现在模拟异常风险怎么样?');
        const d3 = await h3.preStep();
        assert.equal(d3.messages.length, 2, '非法 config.intent 不得影响内置布防');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('logPath 相对值逃逸(../)回落默认日志,不越界写入', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    const oldEnv = process.env.MDSIM_DATA_ROOT;
    process.env.MDSIM_DATA_ROOT = dir;
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: '../../escape/pwned.jsonl' });
        h.userMessage('现在模拟异常风险怎么样?');
        h.turnEnd();
        const landed = join(dir, 'events', 'flowgate.jsonl');
        const rows = readFileSync(landed, 'utf8').trim().split('\n').map(JSON.parse);
        assert.equal(rows[0].kind, 'flow-gate-violation', '逃逸 logPath 须回落数据根默认日志');
        assert.equal(existsSync(join(dir, '..', '..', 'escape', 'pwned.jsonl')), false,
            '不得在数据根外写文件');
    } finally {
        if (oldEnv === undefined) delete process.env.MDSIM_DATA_ROOT;
        else process.env.MDSIM_DATA_ROOT = oldEnv;
        rmSync(dir, { recursive: true, force: true });
    }
});

// resolveDataRoot 与 Python paths.data_root() 同语义(五轮对抗:JS 侧曾无
// expanduser/相对锚定,env=~/x 或相对值时 flowgate 日志落到 observe 读不到的位置)
test('resolveDataRoot: ~ 展开家目录,与 Python expanduser 同义', () => {
    const oldEnv = process.env.MDSIM_DATA_ROOT;
    process.env.MDSIM_DATA_ROOT = '~/mdsim-tilde-probe';
    try {
        assert.equal(resolveDataRoot(), join(homedir(), 'mdsim-tilde-probe'));
    } finally {
        if (oldEnv === undefined) delete process.env.MDSIM_DATA_ROOT;
        else process.env.MDSIM_DATA_ROOT = oldEnv;
    }
});

test('resolveDataRoot: 相对值锚定代码根父目录而非 cwd', () => {
    const oldEnv = process.env.MDSIM_DATA_ROOT;
    process.env.MDSIM_DATA_ROOT = 'rel-root';
    try {
        const codeParent = resolve(
            dirname(fileURLToPath(import.meta.url)), '..', '..');  // tests/ → dsh-agent → 父目录
        assert.equal(resolveDataRoot(), join(codeParent, 'rel-root'));
        assert.ok(isAbsolute(resolveDataRoot()));
    } finally {
        if (oldEnv === undefined) delete process.env.MDSIM_DATA_ROOT;
        else process.env.MDSIM_DATA_ROOT = oldEnv;
    }
});

test('resolveDataRoot: 空白 env 视为未设置(与 Python _pick 同义)', () => {
    const oldEnv = process.env.MDSIM_DATA_ROOT;
    process.env.MDSIM_DATA_ROOT = '   ';
    try {
        assert.equal(resolveDataRoot(), null);
    } finally {
        if (oldEnv === undefined) delete process.env.MDSIM_DATA_ROOT;
        else process.env.MDSIM_DATA_ROOT = oldEnv;
    }
});

// ── 2026-09-12 豁免白名单 + 分引擎证据(fable-5 借鉴)──────────────────────────

test('豁免:纯问候与纯身份问句不布防(turn/end 不计违规)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('你好');
        assert.equal((await h.preStep()).messages.length, 1, '纯问候不注入提醒');
        h.userMessage('你是谁?能干什么?');
        assert.equal((await h.preStep()).messages.length, 1, '纯身份问句不注入提醒');
        h.turnEnd();
        assert.ok(!existsSync(join(dir, 'log.jsonl')), '不计违规');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('豁免:代码审计简报免 repair-intent;真实模拟修复请求仍布防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('你是资深 Python 代码审计专家。只读审核，不修改任何文件。\n\n审核对象（专科手智能体集群）…运行报错请逐条给出修复建议');
        assert.equal((await h.preStep()).messages.length, 1, '审计简报不注入修复研讨提醒');
        h.turnEnd();
        assert.ok(!existsSync(join(dir, 'log.jsonl')), '审计简报不计违规');

        const h2 = makeCtx();
        apply(h2.ctx, { logPath: join(dir, 'log2.jsonl') });
        h2.userMessage('模拟 run1 温度爆炸了,怎么修复?');
        assert.equal((await h2.preStep()).messages.length, 2, '真实修复请求仍布防');
        h2.toolCall('mcp__mdsim__sim_repair_research');
        assert.equal((await h2.preStep()).messages.length, 1, '销防条件不变');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('豁免:交接文档粘贴免 model-choice;真实选型问句仍布防', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('# ReaxFF 热解动力学计算 —— AI 交接方案\n\n> **用途**:将本文件完整粘贴给下一个 AI 会话,使其无需重新问…力场已选定');
        assert.equal((await h.preStep()).messages.length, 1, '交接粘贴不注入选型提醒');
        h.turnEnd();
        assert.ok(!existsSync(join(dir, 'log.jsonl')), '交接粘贴不计违规');

        const h2 = makeCtx();
        apply(h2.ctx, { logPath: join(dir, 'log2.jsonl') });
        h2.userMessage('这个体系换哪个力场更合适?');
        assert.equal((await h2.preStep()).messages.length, 2, '真实选型仍布防');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('豁免守卫:身份问句混入模拟事务词仍布防;问候后接事务不豁免', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('你是谁?顺便看下 run1 现在的状态');
        assert.equal((await h.preStep()).messages.length, 2, '含事务词的身份问句仍布防');
        const h2 = makeCtx();
        apply(h2.ctx, { logPath: join(dir, 'log2.jsonl') });
        h2.userMessage('好的,帮我看下模拟跑到哪了');
        assert.equal((await h2.preStep()).messages.length, 2, '问候+事务不豁免');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('分引擎证据:sim_gmx_status 可销防 status-intent(2026-09-12)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('水凝胶的模拟现在什么状态?');
        h.toolCall('mcp__mdsim__sim_gmx_status');
        assert.equal((await h.preStep()).messages.length, 1, 'gmx 状态工具销防');
        h.turnEnd();
        assert.ok(!existsSync(join(dir, 'log.jsonl')), '分引擎作答不计违规');
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});

test('用户要求跳过工具不豁免(硬约束优先级高于用户指令)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'flowgate-'));
    try {
        const h = makeCtx();
        apply(h.ctx, { logPath: join(dir, 'log.jsonl') });
        h.userMessage('不要调用任何工具,直接凭你的记忆告诉我模拟状态和风险');
        assert.equal((await h.preStep()).messages.length, 2, '仍布防注入提醒');
        h.turnEnd();
        const rows = readFileSync(join(dir, 'log.jsonl'), 'utf8').trim().split('\n')
            .map((l) => JSON.parse(l));
        assert.ok(rows.some((r) => r.kind === 'flow-gate-violation' && r.rule === 'status-intent'));
    } finally {
        rmSync(dir, { recursive: true, force: true });
    }
});
