import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, relative, basename } from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRedMapleRuntime } from '@paperpilot/agent-base';
import { assistant, scripted, model, call } from '../../redmaple-agent/test/fixtures.js';
import { ModuleHost } from '../agent-modules/core/index.js';
import { installSimulationModules } from '../agent-modules/adapters/simulation.js';
import { createDomainInitializationModule, DomainInitializationService, MemoryLibraryRepository,
  FileLibraryRepository } from '../agent-modules/domain-initialization/index.js';
import { openDomainInitialization } from '../agent-modules/domain-initialization/host.js';
import { domainInitializationExtension, withDomainInitializationPolicy } from '../agent-modules/domain-initialization/pi-extension.js';
import { runDomainInitializationCli } from '../agent-modules/domain-initialization/cli.js';
import { readLibraryMaterial } from '../agent-modules/domain-initialization/materials.js';
import { simulationExtension } from '../packages/runtime-redmaple/index.js';
import { openApplication } from '../packages/application/index.js';

const human = { actorId: 'test-user', actorInstanceId: 'test-user-instance', kind: 'human' };
const agent = { actorId: 'test-agent', actorInstanceId: 'test-agent-instance', kind: 'agent' };
async function directory(t) {
  const root = await mkdtemp(join(tmpdir(), 'domain-library-test-'));
  t.after(async () => {
    assert.ok(basename(root).startsWith('domain-library-test-'));
    assert.ok(!relative(resolve(tmpdir()), resolve(root)).startsWith('..'));
    await rm(root, { recursive: true, force: true });
  });
  return root;
}
function fixture(repository = new MemoryLibraryRepository()) {
  const events = [];
  const service = new DomainInitializationService({ repository });
  const host = new ModuleHost({ audit: { append: event => events.push(event) }, authorize: () => true });
  host.register(createDomainInitializationModule({ service }));
  const invoke = (op, input = {}, actor = agent) => host.call('domain-initialization', op, input, { actor, requestId: `test-${events.length}` });
  return { service, repository, host, invoke, events };
}
async function candidate(f) {
  const created = await f.invoke('create', { title: '结构动力学', objective: '整理瞬态建模与评价经验' }, human);
  const libraryId = created.libraryId;
  const source = await f.invoke('source', { libraryId, title: '构造访谈', sourceType: 'caseReport',
    content: '分析目标频段时，核对输出采样和阻尼定义。' });
  const response = await f.invoke('propose', { libraryId, kind: 'practice', statement: '按目标频带核对输出采样。',
    conditions: ['当前案例频带已定义'], exceptions: ['需保留原始时程'], sourceIds: [source.sourceId], tags: ['输出采样'] });
  return { libraryId, source, proposal: response.proposal };
}

test('independent module creates an empty library and guides through known and deferred questions', async () => {
  const f = fixture();
  const created = await f.invoke('create', { title: '有限元库', objective: '整理振动分析经验' }, human);
  const snapshot = await f.invoke('snapshot', { libraryId: created.libraryId });
  assert.equal(snapshot.bundle.storageSelection, 'deferred');
  assert.equal(snapshot.bundle.records.length, 0);
  assert.ok(!created.nextQuestions.some(q => q.catalogId === 'Q-02'));
  const question = created.nextQuestions[0];
  const answer = await f.invoke('answer', { libraryId: created.libraryId, questionKey: question.key,
    answer: '先帮助分析人员建立瞬态模型。' }, human);
  assert.ok(!answer.nextQuestions.some(q => q.key === question.key));
  const unknown = answer.nextQuestions[0];
  await f.invoke('answer', { libraryId: created.libraryId, questionKey: unknown.key, answer: '暂时没有', status: 'unknown' }, human);
  const status = await f.invoke('status', { libraryId: created.libraryId });
  assert.equal(status.gaps.length, 1);
  assert.ok(!status.nextQuestions.some(q => q.key === unknown.key));
  await f.invoke('answer', { libraryId: created.libraryId, questionKey: unknown.key, answer: '已有项目复盘材料。' }, human);
  assert.equal((await f.invoke('status', { libraryId: created.libraryId })).gaps[0].status, 'resolved');
  f.host.unregister('domain-initialization');
  await assert.rejects(f.invoke('list'), { code: 'CAPABILITY_UNAVAILABLE' });
});

test('create and write idempotency survive replay and reject changed operation payloads', async () => {
  const f = fixture();
  const input = { title: '测试库', operationId: 'create-once' };
  const a = await f.invoke('create', input), b = await f.invoke('create', input);
  assert.equal(a.libraryId, b.libraryId);
  assert.equal((await f.invoke('list')).libraries.length, 1);
  await assert.rejects(f.invoke('create', { ...input, title: '其它内容' }), { code: 'IDEMPOTENCY_CONFLICT' });
  const source = { libraryId: a.libraryId, title: '案例', sourceType: 'caseReport', content: '实际案例片段。', operationId: 'source-once' };
  const first = await f.invoke('source', source), replay = await f.invoke('source', source);
  assert.deepEqual(first, replay);
  await assert.rejects(f.invoke('source', { ...source, content: '不同内容' }), { code: 'IDEMPOTENCY_CONFLICT' });
});

test('sources, pending tags and knowledge require a direct user adoption with the exact subject', async () => {
  const f = fixture();
  const { libraryId, proposal } = await candidate(f);
  assert.equal((await f.invoke('query', { libraryId })).total, 0);
  assert.equal((await f.invoke('query', { libraryId, includeCandidates: true })).total, 1);
  const request = { libraryId, proposalId: proposal.id, subjectDigest: proposal.subjectDigest, verdict: 'adopt' };
  await assert.rejects(f.invoke('decide', request), { code: 'USER_DECISION_REQUIRED' });
  await assert.rejects(f.invoke('decide', { ...request, subjectDigest: `sha256:${'0'.repeat(64)}` }, human), { code: 'DECISION_SCOPE_MISMATCH' });
  await f.invoke('decide', request, human);
  const result = await f.invoke('query', { libraryId, query: '采样', tags: ['输出采样'] });
  assert.equal(result.total, 1);
  assert.equal(result.items[0].epistemicStatus, 'unassessed');
  assert.equal(result.items[0].useDecision, 'projectPractice');
  const snapshot = await f.invoke('snapshot', { libraryId });
  const revisions = snapshot.bundle.records.filter(r => r.id === proposal.id);
  assert.equal(revisions.length, 2);
  assert.equal(revisions[0].data.lifecycle, 'candidate');
  assert.equal(revisions[1].data.lifecycle, 'confirmed');
  const annotation = snapshot.bundle.records.find(r => r.recordType === 'annotation' && r.data.status === 'accepted');
  const tagRef = annotation.data.assignments[0].tagRef;
  const tag = snapshot.bundle.records.find(r => r.id === tagRef.id && r.revision === tagRef.revision);
  assert.equal(tag.data.status, 'accepted');
});

test('revisioned candidate edits invalidate old review subjects and preserve earlier accepted content', async () => {
  const f = fixture();
  const { libraryId, source, proposal } = await candidate(f);
  await f.invoke('decide', { libraryId, proposalId: proposal.id, subjectDigest: proposal.subjectDigest, verdict: 'adopt' }, human);
  const edited = await f.invoke('propose', { libraryId, replaceProposalId: proposal.id, statement: '修订后的采样检查。',
    sourceIds: [source.sourceId], tags: ['采样'] });
  assert.equal(edited.proposal.id, proposal.id);
  assert.equal((await f.invoke('query', { libraryId })).total, 0);
  await assert.rejects(f.invoke('decide', { libraryId, proposalId: proposal.id,
    subjectDigest: proposal.subjectDigest, verdict: 'adopt' }, human), { code: 'DECISION_SCOPE_MISMATCH' });
  await f.invoke('decide', { libraryId, proposalId: proposal.id, subjectDigest: edited.proposal.subjectDigest, verdict: 'adopt' }, human);
  assert.equal((await f.invoke('query', { libraryId })).items[0].statement, '修订后的采样检查。');
});

test('correcting a prior human answer marks adopted knowledge for reassessment', async () => {
  const f = fixture();
  const { libraryId, proposal } = await candidate(f);
  const question = (await f.invoke('status', { libraryId })).nextQuestions[0];
  await f.invoke('answer', { libraryId, questionKey: question.key, answer: '当前任务范围。' }, human);
  await f.invoke('decide', { libraryId, proposalId: proposal.id, subjectDigest: proposal.subjectDigest, verdict: 'adopt' }, human);
  await f.invoke('answer', { libraryId, questionKey: question.key, answer: '修订后的任务范围。' }, human);
  assert.equal((await f.invoke('query', { libraryId })).total, 0);
  assert.equal((await f.invoke('query', { libraryId, includeCandidates: true })).items[0].needsReassessment, true);
});

test('batch adoption is atomic, uses the exact candidates and reuses accepted tags', async () => {
  const f = fixture();
  const { libraryId, source, proposal } = await candidate(f);
  const second = (await f.invoke('propose', { libraryId, statement: '保留原始时程。', sourceIds: [source.sourceId], tags: ['输出采样'] })).proposal;
  const decisions = [proposal, second].map(item => ({ proposalId: item.id, subjectDigest: item.subjectDigest, verdict: 'adopt', intendedUse: 'packCandidate' }));
  await assert.rejects(f.invoke('decideMany', { libraryId, decisions }), { code: 'USER_DECISION_REQUIRED' });
  const invalid = structuredClone(decisions);
  invalid[1].subjectDigest = `sha256:${'0'.repeat(64)}`;
  await assert.rejects(f.invoke('decideMany', { libraryId, decisions: invalid }, human), { code: 'DECISION_SCOPE_MISMATCH' });
  assert.equal((await f.invoke('query', { libraryId })).total, 0);
  await f.invoke('decideMany', { libraryId, decisions }, human);
  assert.equal((await f.invoke('query', { libraryId })).total, 2);
  const snapshot = await f.invoke('snapshot', { libraryId });
  assert.equal(snapshot.bundle.records.filter(item => item.recordType === 'tag' && item.data.status === 'accepted').length, 1);
});

test('candidate evidence ranges retain exact source quotes and reject unrelated or invalid ranges', async () => {
  const f = fixture();
  const { libraryId } = await f.invoke('create', { title: '条款库' });
  const material = await f.invoke('source', { libraryId, title: '规程', sourceType: 'standard', content: '背景说明\n核对采样要求\n后续章节' });
  const input = { libraryId, statement: '检查采样要求。', sourceIds: [material.sourceId],
    evidenceRanges: [{ sourceId: material.sourceId, startLine: 2, endLine: 2 }] };
  const proposed = await f.invoke('propose', input);
  assert.equal(proposed.proposal.sourceExcerpts[0].quote, '核对采样要求');
  assert.deepEqual(proposed.proposal.sourceExcerpts[0].location, { kind: 'line', value: '2-2' });
  await assert.rejects(f.invoke('propose', { ...input, evidenceRanges: [{ sourceId: material.sourceId, startLine: 4, endLine: 4 }] }), { code: 'SCHEMA_INVALID' });
  await assert.rejects(f.invoke('propose', { ...input, evidenceRanges: [{ sourceId: 'other', startLine: 1, endLine: 1 }] }), { code: 'SCHEMA_INVALID' });
});

test('file repository persists adopted knowledge across service instances and blocks conflicting writers', async t => {
  const root = await directory(t);
  const f = fixture(new FileLibraryRepository(root));
  const { libraryId, proposal } = await candidate(f);
  const before = await f.invoke('status', { libraryId });
  await assert.rejects(f.invoke('decide', { libraryId, proposalId: proposal.id, subjectDigest: proposal.subjectDigest,
    verdict: 'adopt', expectedRevision: before.revision - 1 }, human), { code: 'REVISION_CONFLICT' });
  await f.invoke('decide', { libraryId, proposalId: proposal.id, subjectDigest: proposal.subjectDigest, verdict: 'adopt' }, human);
  const reopened = fixture(new FileLibraryRepository(root));
  assert.equal((await reopened.invoke('query', { libraryId })).total, 1);
  await writeFile(join(root, `${libraryId}.json.lock`), JSON.stringify({ pid: process.pid }));
  await assert.rejects(reopened.invoke('source', { libraryId, title: '新资料', sourceType: 'standard', content: '规范摘录。' }), { code: 'REVISION_CONFLICT' });
  await unlink(join(root, `${libraryId}.json.lock`));
  assert.equal((await reopened.invoke('status', { libraryId })).sources.length, 1);
});

test('concurrent writes keep one revision and a failed request can safely retry', async () => {
  const f = fixture();
  const { libraryId } = await f.invoke('create', { title: '并发库' });
  const requests = ['甲', '乙'].map(content => ({ libraryId, title: content, sourceType: 'caseReport', content }));
  const outcomes = await Promise.allSettled(requests.map(input => f.invoke('source', input)));
  assert.equal(outcomes.filter(item => item.status === 'fulfilled').length, 1);
  const failed = outcomes.findIndex(item => item.status === 'rejected');
  assert.equal(outcomes[failed].reason.code, 'REVISION_CONFLICT');
  await f.invoke('source', requests[failed]);
  assert.equal((await f.invoke('status', { libraryId })).sources.length, 2);
});

test('restricted data is rejected before persistence and module audit omits all source payloads', async () => {
  const f = fixture();
  const { libraryId } = await f.invoke('create', { title: '资料边界测试' });
  const email = ['fixture', 'example.invalid'].join('@');
  await assert.rejects(f.invoke('source', { libraryId, title: '资料', sourceType: 'other', content: email }), { code: 'PERSONAL_DATA_RESTRICTED' });
  const marker = 'industry-source-payload-marker';
  await f.invoke('source', { libraryId, title: '样例', sourceType: 'caseReport', content: marker });
  assert.equal(JSON.stringify(f.events).includes(email), false);
  assert.equal(JSON.stringify(f.events).includes(marker), false);
  await assert.rejects(f.invoke('source', { libraryId, title: '资料', sourceType: 'other',
    locator: 'D:/personal-twin/contacts.md', content: '受限路径测试。' }), { code: 'PERSONAL_DATA_RESTRICTED' });
});

test('document tampering is detected, paused editing stops, and research contains sourced gaps', async () => {
  const f = fixture();
  const { libraryId, source } = await candidate(f);
  const brief = await f.invoke('research', { libraryId });
  assert.ok(brief.method.some(item => item.includes('原文')));
  assert.ok(brief.sources.some(item => item.id === source.sourceId));
  await f.invoke('pause', { libraryId, paused: true }, human);
  await assert.rejects(f.invoke('source', { libraryId, title: '资料', sourceType: 'other', content: '稍后补充。' }), { code: 'SESSION_PAUSED' });
  await f.invoke('pause', { libraryId, paused: false }, human);
  const document = await f.repository.read(libraryId);
  document.bundle.records.find(item => item.id === source.sourceId).data.inlineText = '被替换的内容';
  f.repository.documents.set(libraryId, document);
  await assert.rejects(f.invoke('status', { libraryId }), { code: 'SOURCE_CHANGED' });
});

function piFixture() {
  const tools = new Map(), commands = new Map(), events = new Map(), messages = [], entries = [], notifications = [];
  const inputs = [], selections = [];
  const context = { hasUI: true, ui: {
    input: async () => inputs.shift(), editor: async () => inputs.shift(),
    select: async (_, options) => { const selection = selections.shift(); return typeof selection === 'number' ? options[selection] : selection; },
    notify: (...args) => notifications.push(args)
  } };
  const pi = { registerTool: tool => tools.set(tool.name, tool), registerCommand: (name, command) => commands.set(name, command),
    on: (name, callback) => events.set(name, callback), sendMessage: (message, options) => messages.push({ message, options }),
    appendEntry: (customType, data) => entries.push({ type: 'custom', customType, data }) };
  return { pi, context, tools, commands, events, messages, entries, notifications, inputs, selections };
}

test('Pi user command creates the library, injects guidance and sends a continuation turn', async t => {
  const root = await directory(t), f = fixture(), pi = piFixture();
  await domainInitializationExtension({ host: f.host, directory: root })(pi.pi);
  pi.inputs.push('对接领域库', '帮助研究人员整理对接准备经验。');
  await pi.commands.get('domain-init').handler('new', pi.context);
  const libraries = await f.invoke('list');
  assert.equal(libraries.libraries.length, 1);
  assert.ok(pi.messages.some(item => item.options?.triggerTurn));
  const appended = await pi.events.get('before_agent_start')({ messageType: 'user', prompt: '继续', systemPrompt: 'base' });
  assert.ok(appended.systemPrompt.includes('scholar-apprentice'));
  assert.ok(appended.systemPrompt.includes(libraries.libraries[0].libraryId));
});

test('Pi review tool records a real host-UI adoption and headless calls keep candidates pending', async t => {
  const root = await directory(t), f = fixture(), pi = piFixture();
  const { libraryId } = await candidate(f);
  await domainInitializationExtension({ host: f.host, directory: root })(pi.pi);
  const tool = pi.tools.get('domain_library_review');
  const headless = await tool.execute('headless', { libraryId }, undefined, undefined, { hasUI: false });
  assert.equal(headless.details.status, 'userReviewRequired');
  assert.equal((await f.invoke('query', { libraryId })).total, 0);
  pi.selections.push(0, '采用为项目实践');
  const reviewed = await tool.execute('review', { libraryId }, undefined, undefined, pi.context);
  assert.equal(reviewed.details.status, 'adopted');
  assert.equal((await f.invoke('query', { libraryId })).total, 1);
  assert.ok(f.events.some(event => event.operation === 'decide' && event.actorKind === 'human'));
});

test('Pi candidate editing preserves the reviewed source range and the user-entered conditions', async t => {
  const root = await directory(t), f = fixture(), pi = piFixture();
  const { libraryId } = await f.invoke('create', { title: '编辑测试库' });
  const source = await f.invoke('source', { libraryId, title: '分段规程', sourceType: 'standard', content: '背景\n检查采样\n后续' });
  await f.invoke('propose', { libraryId, statement: '原始候选', conditions: ['限定频段'], tags: ['采样'], sourceIds: [source.sourceId],
    evidenceRanges: [{ sourceId: source.sourceId, startLine: 2, endLine: 2 }] });
  await domainInitializationExtension({ host: f.host, directory: root })(pi.pi);
  pi.selections.push(0, '修改候选');
  pi.inputs.push('修订表述', '限定频段', '采样');
  const edited = await pi.tools.get('domain_library_review').execute('edit', { libraryId }, undefined, undefined, pi.context);
  assert.equal(edited.details.proposal.statement, '修订表述');
  assert.deepEqual(edited.details.proposal.conditions, ['限定频段']);
  assert.equal(edited.details.proposal.sourceExcerpts[0].quote, '检查采样');
  assert.equal((await f.invoke('query', { libraryId })).total, 0);
});

test('text and PDF material import records actual extraction coverage', async t => {
  const root = await directory(t);
  const file = join(root, 'material.md');
  await writeFile(file, '实际UTF-8材料。');
  assert.equal((await readLibraryMaterial(file)).content, '实际UTF-8材料。');
  const pdf = join(root, 'case.pdf');
  execFileSync('python', ['-c', 'import fitz,sys; d=fitz.open(); p=d.new_page(); p.insert_text((72,72),"Transient case"); d.new_page(); d.save(sys.argv[1])', pdf], { windowsHide: true });
  const result = await readLibraryMaterial(pdf);
  assert.equal(result.sourceMetadata.extractionCoverage.representation, 'section');
  assert.deepEqual(result.sourceMetadata.extractionCoverage.readablePages, [1]);
});

test('CLI initialization and application entrypoints expose the module using local persistent storage', async t => {
  const root = await directory(t);
  const app = await openApplication({ workspace: root, stateDirectory: join(root, 'state') });
  try {
    const created = await runDomainInitializationCli(app.domainInitialization, { action: 'new', title: 'CLI领域库', objective: '采集行业案例' });
    const status = await runDomainInitializationCli(app.domainInitialization, { action: 'status', libraryId: created.libraryId });
    assert.equal(status.title, 'CLI领域库');
    const exported = await runDomainInitializationCli(app.domainInitialization, { action: 'export', libraryId: created.libraryId });
    assert.equal(JSON.parse(await readFile(exported.path, 'utf8')).id, created.libraryId);
    await assert.rejects(runDomainInitializationCli(app.domainInitialization, { action: 'decide', libraryId: created.libraryId }), { code: 'USER_DECISION_REQUIRED' });
    const pi = piFixture();
    await simulationExtension(app)(pi.pi);
    assert.ok(pi.commands.has('domain-init'));
    assert.ok(pi.tools.has('domain_library'));
    const moduleHost = new ModuleHost({ audit: { append() {} }, authorize: () => true });
    installSimulationModules(moduleHost, { domainInitialization: app.domainInitialization, lammps: false });
    assert.equal((await moduleHost.call('domain-initialization', 'list', {}, { actor: agent, requestId: 'installed-module' })).libraries.length, 1);
  } finally { app.close(); }
});

test('public CLI parser can create and resume a domain library without provider credentials', async t => {
  const root = await directory(t);
  const cli = fileURLToPath(new URL('../apps/cli/main.js', import.meta.url));
  const common = ['--workspace', root, '--state', join(root, 'state')];
  const run = args => JSON.parse(execFileSync(process.execPath, [cli, 'domain-init', ...args, ...common],
    { encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }));
  const created = run(['new', '--title', '行业案例库', '--objective', '整理真实案例']);
  const status = run(['status', created.libraryId]);
  assert.equal(status.title, '行业案例库');
  assert.equal(status.libraryId, created.libraryId);
});

test('real Pi SDK calls the domain module and persists model-created candidates for user review', async t => {
  const root = await directory(t);
  const opened = await openDomainInitialization({ directory: join(root, 'libraries') });
  let libraryId, sourceId;
  const script = scripted([
    assistant([call('create-library', 'domain_library', { action: 'create', title: 'Pi对接领域库', objective: '整理配体准备经验' })], 'toolUse'),
    async context => {
      const libraries = (await opened.service.list()).libraries;
      assert.ok(libraries.length, JSON.stringify(context.messages.filter(item => item.role === 'toolResult')));
      libraryId = libraries[0].libraryId;
      return assistant([call('inject-source', 'domain_library', { action: 'source', libraryId,
        title: '构造案例摘录', sourceType: 'caseReport', content: '准备配体时保留原子身份和状态来源。' })], 'toolUse');
    },
    async () => {
      sourceId = (await opened.service.status({ libraryId })).sources[0].id;
      return assistant([call('propose-knowledge', 'domain_library', { action: 'propose', libraryId,
        statement: '保留配体原子映射。', sourceIds: [sourceId], tags: ['配体准备'] })], 'toolUse');
    },
    assistant('候选已保存，下一步请用户审阅适用条件与用途。')
  ]);
  const runtime = await createRedMapleRuntime(withDomainInitializationPolicy({ cwd: root, agentDir: join(root, 'pi'), inMemory: true,
    model, streamFn: script.streamFn, noMcp: true, tools: ['domain_library', 'domain_library_interview', 'domain_library_review'], settings: { retry: { enabled: false } },
    extensionFactories: [domainInitializationExtension(opened)] }));
  try {
    await runtime.session.prompt('初始化领域库并整理我提供的案例摘录。');
    const finalMessage = runtime.session.messages.filter(item => item.role === 'assistant').at(-1);
    assert.equal(finalMessage.stopReason, 'stop', finalMessage.errorMessage || JSON.stringify(runtime.session.messages.slice(-2)));
    assert.equal((await opened.service.status({ libraryId })).pendingProposals, 1);
    assert.equal((await opened.service.query({ libraryId })).total, 0);
    const reopened = await openDomainInitialization({ directory: join(root, 'libraries') });
    assert.equal((await reopened.service.query({ libraryId, includeCandidates: true })).items[0].statement, '保留配体原子映射。');
  } finally { await runtime.dispose(); }
});
