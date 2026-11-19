// dsh-md-ops-gate — force designated tools (the MD-sim audited write gate
// `mcp__mdsim__sim_exec`) through the dsh approval seam. The web profile
// composes a UI answerer, so the user gets an approve/reject popup carrying
// the command and its stated purpose; headless has no answerer and the seam
// fails closed, so writes are impossible there.
//
// 2026-09-20 重发免重复弹窗: 看板红区卡批准后, agent 原样重发的同一命令会在
// 这里再次弹窗——同一操作点两次按钮。放行前先查看板「该 cmd 是否有人工批准
// 且未消费的闸卡」: 有就直接放行去 kit 闸(kit 侧 sha256 哈希绑定才是真执行闸,
// cmd 被改动则查不到批准、依旧弹窗; 看板不可达也依旧弹窗——失败关闭)。
export const name = 'dsh-md-ops-gate';

// 2026-11-16: 工具名随部署 serverName 变化(evidence-v3 profile 用 mdagent),
// 默认同时覆盖两种命名;装了却永不命中是最坏的中间态。
const DEFAULT_GATED = ['mcp__mdsim__sim_exec', 'mcp__mdagent__sim_exec'];

export function apply(ctx, config) {
    const gated = new Set(config?.tools ?? DEFAULT_GATED);
    const board = (config?.boardUrl ?? 'http://127.0.0.1:8080').replace(/\/+$/, '');
    const boardToken = typeof config?.boardToken === 'string' ? config.boardToken : '';
    ctx.on('tools/pre-execute', async (exec, next) => {
        if (!gated.has(exec.name)) return next();
        const args = exec.arguments ?? {};
        const cmd = typeof args.cmd === 'string' ? args.cmd : JSON.stringify(args);
        try {
            let url = board + '/api/exec_gate_lookup?cmd='
                + encodeURIComponent(cmd.slice(0, 2000));
            if (boardToken) url += '&token=' + encodeURIComponent(boardToken);
            const r = await fetch(url, { signal: AbortSignal.timeout(4000) });
            if (r.ok) {
                const d = await r.json();
                if (d && d.approved_unconsumed === true) return next();
            }
        } catch { /* 看板不可达/响应异常 → 回人工弹窗,不因省事放开 */ }
        const desc = typeof args.description === 'string' ? args.description : '';
        return {
            kind: 'ask',
            reason: [
                '分子模拟写操作(经 claude_interface 审计通道, 逐条留痕)',
                `命令: ${cmd}`,
                `目的: ${desc || '(未填写)'}`,
                '批准 = 执行这一次; 拒绝 = 不执行。'
            ].join('\n')
        };
    });
}
