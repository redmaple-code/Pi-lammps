import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, get } from 'node:http';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { openDomainInitialization } from '../agent-modules/domain-initialization/host.js';
import { openDomainWeb } from '../apps/domain-web/server.js';

async function workbench(t, configPath) {
  const root = await mkdtemp(join(tmpdir(), 'domain-web-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const library = await openDomainInitialization({ directory: root });
  const web = await openDomainWeb({ ...library, port: 0, configPath });
  t.after(() => new Promise(resolve => web.server.close(resolve)));
  const { token, modelAvailable } = await (await fetch(web.origin + '/api/session')).json();
  const request = (operation, input, headers = {}) => fetch(web.origin + '/api/' + operation, {
    method: 'POST', body: JSON.stringify(input),
    headers: { 'Content-Type': 'application/json', 'X-Domain-Session': token, ...headers }
  });
  const call = async (operation, input) => {
    const response = await request(operation, input);
    const value = await response.json();
    assert.equal(response.status, 200, JSON.stringify(value));
    return value;
  };
  return { request, call, modelAvailable, root, origin: web.origin, ...library };
}

test('web operations share the persisted library, enforce local requests and record exact review decisions', async t => {
  const { request, call, service, modelAvailable, root, origin } = await workbench(t);
  assert.equal(modelAvailable, false);
  const input = { title: '结构动力学练习', objective: '记录瞬态结果采样经验', operationId: randomUUID() };
  assert.equal((await request('create', input, { Origin: 'https://other.invalid' })).status, 403);
  assert.equal((await request('create', input, { 'X-Domain-Session': 'invalid' })).status, 403);
  await new Promise((resolve, reject) => {
    get(origin + '/api/session', { headers: { Host: 'other.invalid' } }, response => {
      response.resume();
      response.on('end', () => {
        try { assert.equal(response.statusCode, 403); resolve(); } catch (error) { reject(error); }
      });
    }).on('error', reject);
  });
  assert.equal((await service.list()).libraries.length, 0);

  const created = await call('create', input);
  const identity = { libraryId: created.libraryId };
  const source = await call('upload', { ...identity, name: 'case.md', title: '台架记录', sourceType: 'caseReport',
    base64: Buffer.from('模型准备\n比较输出采样间隔\n边界条件').toString('base64'),
    expectedRevision: created.revision, operationId: randomUUID() });
  const preview = await call('readSource', { ...identity, sourceId: source.sourceId, startLine: 2, endLine: 2 });
  assert.equal(preview.inlineText, '比较输出采样间隔');
  const proposed = await call('propose', { ...identity, expectedRevision: source.revision, operationId: randomUUID(),
    statement: '对输出采样间隔做独立性对照', tags: ['采样'], sourceIds: [source.sourceId],
    evidenceRanges: [{ sourceId: source.sourceId, startLine: 2, endLine: 2 }] });
  assert.equal((await call('query', identity)).total, 0);
  const decision = { ...identity, expectedRevision: proposed.revision, operationId: randomUUID(),
    proposalId: proposed.proposal.id, subjectDigest: proposed.proposal.subjectDigest, verdict: 'adopt',
    intendedUse: 'projectPractice' };
  assert.equal((await request('decide', { ...decision, expectedRevision: source.revision })).status, 409);
  assert.equal((await call('decide', decision)).status, 'adopted');
  const reopened = await openDomainInitialization({ directory: root });
  const { items } = await reopened.service.query({ ...identity, tags: ['采样'] });
  assert.equal(items[0].useDecision, 'projectPractice');
  assert.equal((await call('snapshot', identity)).id, created.libraryId);
});

test('web drafts send only the selected excerpt and wait for an explicit candidate submission', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'domain-web-model-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const fixtureName = 'DOMAIN_WEB_TEST_KEY';
  process.env[fixtureName] = 'test-only';
  t.after(() => { delete process.env[fixtureName]; });
  const payloads = [];
  const model = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    payloads.push(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    const draft = { statement: '检查采样设置', conditions: ['同一载荷'], exceptions: [], tags: ['采样'] };
    response.writeHead(200, { 'Content-Type': 'text/event-stream' });
    for (const [delta, finish_reason] of [[{ role: 'assistant', content: JSON.stringify(draft) }, null], [{}, 'stop']]) {
      response.write('data: ' + JSON.stringify({ id: 'fixture', created: 1, model: 'fixture', choices: [{ index: 0, delta, finish_reason }] }) + '\n\n');
    }
    response.end('data: [DONE]\n\n');
  });
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => model.close(resolve)));
  const path = join(directory, 'runtime.json');
  await writeFile(path, JSON.stringify({ model: 'test/fixture', providerConnection: {
    provider: 'test', modelId: 'fixture', baseUrl: 'http://127.0.0.1:' + model.address().port + '/v1', apiKeyEnv: fixtureName
  } }));
  const { call, modelAvailable } = await workbench(t, path);
  assert.equal(modelAvailable, true);
  const { libraryId } = await call('create', { title: '本机私有标题', objective: '记录领域经验' });
  const { sourceId } = await call('source', { libraryId, title: '材料', sourceType: 'caseReport',
    content: '窗口外文本\n选中的采样摘录\n下一段' });
  const draft = await call('suggest', { libraryId, sourceId, startLine: 2, endLine: 2 });
  assert.equal(draft.statement, '检查采样设置');
  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].messages[0].role, 'system');
  assert.match(payloads[0].messages[0].content, /仅返回一个 JSON 对象/);
  assert.deepEqual(payloads[0].messages.filter(message => message.role === 'user').flatMap(message => message.content),
    [{ type: 'text', text: '选中的采样摘录' }]);
  assert.equal((await call('status', { libraryId })).proposals.length, 0);
});

test('web source pages preserve long lines and continue into the next line range', async t => {
  const { call, request } = await workbench(t);
  const { libraryId } = await call('create', { title: '长原文练习库' });
  const lines = ['首行', 'a'.repeat(15996) + '🧪长行续文', ...Array.from({ length: 99 }, (_, index) => '记录 ' + (index + 3))];
  const { sourceId } = await call('source', { libraryId, title: '包含长行的资料', sourceType: 'log', content: lines.join('\n') });
  const input = { libraryId, sourceId, startLine: 1 };
  const first = await call('readSource', { ...input, page: 0 });
  const second = await call('readSource', { ...input, page: 1 });
  assert.ok(first.inlineText.endsWith('🧪'));
  assert.equal(second.displayStartLine, 2);
  assert.equal(first.inlineText + second.inlineText, lines.slice(0, 100).join('\n'));
  assert.equal(first.hasMorePages, true);
  assert.equal(second.hasMorePages, false);
  assert.equal(second.hasMoreLines, true);
  const last = await call('readSource', { ...input, startLine: first.endLine + 1, page: 0 });
  assert.equal(last.inlineText, '记录 101');
  assert.equal(last.hasMoreLines, false);
  assert.equal((await request('readSource', { ...input, page: 2 })).status, 400);
});
