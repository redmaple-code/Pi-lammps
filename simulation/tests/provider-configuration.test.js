import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createRedMapleRuntime } from '@paperpilot/agent-base';
import { fixture } from './fixtures.js';
import { loadSimulationRuntimeConfig, withModelSelectionPolicy } from '../packages/runtime-redmaple/configuration.js';

async function configFile(root) {
  const file = join(root, 'runtime.json');
  await writeFile(file, JSON.stringify({ model: 'aispot-ds/deepseek-v4.1-flash', allowedModels: ['aispot-ds/deepseek-v4.1-flash'],
    noMcp: true, thinkingLevel: 'off', providerConnection: { provider: 'aispot-ds', modelId: 'deepseek-v4.1-flash',
      baseUrlEnv: 'AISPOT_DS_BASE_URL', apiKeyEnv: 'AISPOT_DS_API_KEY', reasoning: true,
      compat: { supportsStore: false, supportsDeveloperRole: false, maxTokensField: 'max_tokens',
        requiresReasoningContentOnAssistantMessages: true, thinkingFormat: 'deepseek' } } }));
  return file;
}

test('provider configuration requires the explicit endpoint and credentials', async t => {
  const f = await fixture(t);
  const file = await configFile(f.root);
  await assert.rejects(loadSimulationRuntimeConfig(file, { environment: {} }), { code: 'CAPABILITY_UNAVAILABLE' });
  await assert.rejects(loadSimulationRuntimeConfig(file, { environment: { AISPOT_DS_BASE_URL: 'https://example.invalid/v1' } }), { code: 'AUTHORIZATION_REQUIRED' });
});

test('model selection preserves the host callback and blocks a different provider/model before sending', async () => {
  let authorized = 0;
  const options = withModelSelectionPolicy({ allowedModels: ['aispot-ds/deepseek-v4.1-flash'], beforeModelCall: async () => { authorized++; } });
  await options.beforeModelCall({ model: { provider: 'aispot-ds', id: 'deepseek-v4.1-flash' } });
  assert.equal(authorized, 1);
  await assert.rejects(options.beforeModelCall({ model: { provider: 'openai', id: 'gpt-6-sol' } }), { code: 'model_restricted', knownOutcome: true });
  assert.equal(authorized, 1);
});

test('shipped AISpot configuration applies the 40K window and explicit automatic-compaction reserves', async () => {
  const options = await loadSimulationRuntimeConfig(new URL('../runtime.example.json', import.meta.url), {
    environment: { AISPOT_DS_API_KEY: 'provider-test-only' },
  });
  const selected = options.providers['aispot-ds'].models[0];
  assert.equal(selected.contextWindow, 40960);
  assert.equal(options.settings.compaction.enabled, true);
  assert.ok(options.settings.compaction.reserveTokens >= options.maxOutputTokens + 2048);
  assert.ok(options.settings.compaction.keepRecentTokens <= selected.contextWindow / 4);
  assert.ok(options.settings.compaction.reserveTokens + options.settings.compaction.keepRecentTokens < selected.contextWindow);
});

test('real Pi HTTP transport sends the registered dsv4.1f model and provider credential', async t => {
  const f = await fixture(t);
  const received = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push({ body: JSON.parse(Buffer.concat(chunks).toString()), authorization: request.headers.authorization, url: request.url });
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    for (const [delta, finish_reason, usage] of [
      [{ role: 'assistant', content: 'SIMULATION_PROVIDER_READY' }, null, undefined],
      [{}, 'stop', { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 }]
    ]) response.write(`data: ${JSON.stringify({ id: 'provider-fixture', object: 'chat.completion.chunk', created: 1,
      model: 'deepseek-v4.1-flash', choices: [{ index: 0, delta, finish_reason }], ...(usage ? { usage } : {}) })}\n\n`);
    response.end('data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const file = await configFile(f.root);
  const options = await loadSimulationRuntimeConfig(file, { environment: {
    AISPOT_DS_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`, AISPOT_DS_API_KEY: 'provider-test-only'
  } });
  const runtime = await createRedMapleRuntime({ ...options, cwd: f.root, agentDir: join(f.root, 'agent'),
    inMemory: true, tools: [], settings: { retry: { enabled: false } },
    fetch: async (...args) => { received.push({ fetchHook: true }); return globalThis.fetch(...args); } });
  try {
    await runtime.session.prompt('Check provider connection');
    assert.equal(runtime.session.model.provider, 'aispot-ds');
    assert.equal(runtime.session.model.id, 'deepseek-v4.1-flash');
    assert.equal(received.filter(item => item.fetchHook).length, 1);
    const request = received.find(item => item.body);
    assert.equal(request.body.model, 'deepseek-v4.1-flash');
    assert.equal(request.body.thinking.type, 'disabled');
    assert.equal(request.body.max_tokens, 4096);
    assert.equal(request.body.max_completion_tokens, undefined);
    assert.equal(request.authorization, 'Bearer provider-test-only');
    assert.equal(request.url, '/v1/chat/completions');
    const answer = runtime.session.messages.filter(message => message.role === 'assistant').at(-1);
    assert.equal(answer.stopReason, 'stop', answer.errorMessage);
    assert.ok(answer.content.some(item => item.text === 'SIMULATION_PROVIDER_READY'));
  } finally { await runtime.dispose(); }
});

test('native compaction uses the configured fetch transport and the same model ledger', async t => {
  const f = await fixture(t);
  const thinkingModes = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    thinkingModes.push(JSON.parse(Buffer.concat(chunks).toString('utf8')).thinking?.type);
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    response.write(`data: ${JSON.stringify({ id: 'compaction-transport', object: 'chat.completion.chunk',
      choices: [{ index: 0, delta: { role: 'assistant', content: 'Summary preserves the task and evidence.' }, finish_reason: null }] })}\n\n`);
    response.write(`data: ${JSON.stringify({ id: 'compaction-transport', object: 'chat.completion.chunk',
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 8, total_tokens: 18 } })}\n\n`);
    response.end('data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const options = await loadSimulationRuntimeConfig(await configFile(f.root), { environment: {
    AISPOT_DS_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`, AISPOT_DS_API_KEY: 'provider-test-only'
  } });
  let fetches = 0;
  const runtime = await createRedMapleRuntime({ ...options, cwd: f.root, agentDir: join(f.root, 'agent'),
    thinkingLevel: 'low', compactionThinkingLevel: 'off',
    inMemory: true, tools: [], resources: { noExtensions: true, noSkills: true, noPromptTemplates: true },
    fetch: async (...args) => { fetches++; return globalThis.fetch(...args); },
    settings: { retry: { enabled: false }, compaction: { enabled: false, keepRecentTokens: 100, reserveTokens: 1024 } } });
  try {
    await runtime.session.prompt('original task and evidence '.repeat(150));
    await runtime.session.prompt('follow-up evidence '.repeat(100));
    await runtime.session.compact();
    assert.equal(fetches, 3);
    assert.deepEqual(thinkingModes, ['enabled', 'enabled', 'disabled']);
    const calls = Object.values(runtime.redmaple.controls.ledger.snapshot().calls);
    assert.equal(calls.filter(call => call.purpose === 'compaction' && call.status === 'succeeded').length, 1);
  } finally { await runtime.dispose(); }
});
