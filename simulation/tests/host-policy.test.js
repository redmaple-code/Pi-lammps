import test from 'node:test';
import assert from 'node:assert/strict';
import { createRedMapleRuntime } from '@paperpilot/agent-base';
import { join } from 'node:path';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { model, scripted, assistant, call } from '../../redmaple-agent/test/fixtures.js';

test('host policy applies to actual native tool execution before a read-only annotation can bypass role scope', async t => {
  const root = await mkdtemp(join(tmpdir(), 'simulation-policy-'));
  let executed = false, observed = false;
  const script = scripted([assistant([call('attempt', 'forbidden_service')], 'toolUse'), assistant('Host rejected unauthorized operation')]);
  const runtime = await createRedMapleRuntime({ cwd: root, agentDir: join(root, 'agent'), inMemory: true, model,
    streamFn: script.streamFn, noMcp: true, tools: ['forbidden_service'],
    extensionFactories: [async pi => pi.registerTool({ name: 'forbidden_service', label: 'Service', description: 'Test scope check',
      parameters: { type: 'object', properties: {} }, annotations: { readOnlyHint: true },
      execute: async () => { executed = true; return { content: [{ type: 'text', text: 'unexpected' }] }; } })],
    authorizeTool: async ({ definition }) => { if (definition.name === 'forbidden_service') { observed = true; throw new Error('ROLE_SCOPE_DENIED'); } } });
  t.after(() => runtime.dispose());
  await runtime.session.prompt('Attempt a service call');
  assert.equal(observed, true);
  assert.equal(executed, false);
  assert.ok(JSON.stringify(runtime.session.messages).includes('ROLE_SCOPE_DENIED'));
});
