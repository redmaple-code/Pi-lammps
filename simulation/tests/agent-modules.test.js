import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir, cp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ModuleHost } from '../agent-modules/core/index.js';
import { SqliteAudit } from '../agent-modules/core/audit-sqlite.js';
import { createConsistencyModule } from '../agent-modules/consistency/index.js';
import { createKnowledgeModule } from '../agent-modules/knowledge/index.js';
import { installSimulationModules } from '../agent-modules/adapters/simulation.js';
import { fixture } from './fixtures.js';
import { KnowledgeService } from '../packages/knowledge/index.js';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const actor = { actorId: 'integration-agent', actorInstanceId: 'test-instance', kind: 'agent', capabilities: ['review.submit'] };
const ctx = { actor, requestId: 'request-1' };
async function makeHost(t, authorize = () => true) {
  const root = await mkdtemp(join(tmpdir(), 'agent-modules-'));
  const audit = new SqliteAudit(join(root, 'audit.sqlite'));
  t.after(() => audit.close());
  return { root, audit, host: new ModuleHost({ audit, authorize }) };
}

test('package exports load in a separate host without the simulation tree', async t => {
  const { root } = await makeHost(t);
  const target = join(root, 'node_modules', '@redmaple', 'agent-modules');
  await mkdir(target, { recursive: true });
  await cp(fileURLToPath(new URL('../agent-modules/', import.meta.url)), target, { recursive: true,
    filter: source => !source.endsWith('adapters') });
  const script = `
import { ModuleHost } from '@redmaple/agent-modules/core';
import { createKnowledgeModule } from '@redmaple/agent-modules/knowledge';
import { createEvidenceModule } from '@redmaple/agent-modules/evidence';
import { createGateModule } from '@redmaple/agent-modules/gate';
import { createConsistencyModule } from '@redmaple/agent-modules/consistency';
const host = new ModuleHost({audit:{append(){}},authorize:()=>true});
host.register(createKnowledgeModule({backend:{search:()=>({items:['ok']}),read:()=>({})}}));
const result=await host.call('knowledge','search',{query:'x'},{requestId:'host-1',actor:{actorId:'a',actorInstanceId:'b',kind:'agent'}});
if(result.items[0]!=='ok'||![createEvidenceModule,createGateModule,createConsistencyModule].every(x=>typeof x==='function')) process.exit(1);
`;
  await writeFile(join(root, 'consumer.mjs'), script);
  const proc = spawnSync(process.execPath, [join(root, 'consumer.mjs')], { encoding: 'utf8', windowsHide: true });
  assert.equal(proc.status, 0, proc.stderr);
});

test('four independently built packages plug into a host with shared protocol', async t => {
  const { root } = await makeHost(t);
  const dest = join(root, 'built');
  const builder = fileURLToPath(new URL('../agent-modules/build-bundles.js', import.meta.url));
  const build = spawnSync(process.execPath, [builder, dest], { encoding: 'utf8', windowsHide: true });
  assert.equal(build.status, 0, build.stderr);
  for (const name of ['knowledge','evidence','gate','consistency']) {
    const target = join(root, 'node_modules', '@redmaple', `agent-${name}`);
    await mkdir(target, { recursive: true }); await cp(join(dest, name), target, { recursive: true });
  }
  const script = `
import {ModuleHost} from '@redmaple/agent-knowledge/core';
import {createKnowledgeModule} from '@redmaple/agent-knowledge';
import {createEvidenceModule} from '@redmaple/agent-evidence';
import {createGateModule} from '@redmaple/agent-gate';
import {createConsistencyModule} from '@redmaple/agent-consistency';
const host=new ModuleHost({audit:{append(){}},authorize:()=>true});
host.register(createKnowledgeModule({backend:{search:()=>({items:['ok']}),read:()=>({})}}));
host.register(createEvidenceModule({backend:{read:()=>({}),assess:()=>({})},resolveSource:()=>null}));
host.register(createGateModule({backend:{submit:()=>({})}}));
host.register(createConsistencyModule({inspect:()=>[],checkSetVersion:'1',requiredCheckIds:['one']}));
const context={requestId:'1',actor:{actorId:'a',actorInstanceId:'b',kind:'agent'}};
if(host.list().length!==4) process.exit(1);
if((await host.call('knowledge','search',{query:'hello'},context)).items[0]!=='ok') process.exit(2);
if((await host.call('consistency','inspect',{text:'x'},context)).verdict!=='REVIEW') process.exit(3);
try {await host.call('gate','submit',{checks:[],subjectRefs:[]},context);process.exit(4)} catch(e) {if(e.code!=='SCHEMA_INVALID'||e.sideEffectState!=='none')process.exit(5)}
host.unregister('knowledge');if(host.list().length!==3)process.exit(6);
`;
  await writeFile(join(root, 'independent.mjs'), script);
  const run = spawnSync(process.execPath, [join(root, 'independent.mjs')], { encoding: 'utf8', windowsHide: true });
  assert.equal(run.status, 0, run.stderr);
});

test('portable modules install, deny unauthorized operations and can be unplugged', async t => {
  const { host, audit } = await makeHost(t, (_actor, capability) => capability !== 'knowledge.read');
  let touched = false;
  host.register(createKnowledgeModule({ backend: {
    search: async query => ({ items: [query] }), read: async () => { touched = true; return {}; }
  } }));
  assert.deepEqual((await host.call('knowledge', 'search', { query: 'materials' }, ctx)).items, ['materials']);
  await assert.rejects(host.call('knowledge', 'read', { sourceId: 'item' }, ctx), { code: 'PERMISSION_DENIED', sideEffectState: 'none' });
  assert.equal(touched, false);
  assert.equal(host.unregister('knowledge'), true);
  await assert.rejects(host.call('knowledge', 'search', { query: 'a' }, ctx), { code: 'CAPABILITY_UNAVAILABLE' });
  assert.deepEqual(audit.verify().count, 4);
});

test('consistency validates coverage, unknown results, duplicate IDs and actual LAMMPS adapter', async t => {
  const { host } = await makeHost(t);
  host.register(createConsistencyModule({ inspect: () => [], checkSetVersion: 'v1', requiredCheckIds: ['A'] }));
  let result = await host.call('consistency', 'inspect', { text: 'run 1' }, ctx);
  assert.equal(result.verdict, 'REVIEW'); assert.deepEqual(result.missingCheckIds, ['A']);
  host.unregister('consistency');
  host.register(createConsistencyModule({ inspect: () => [{ checkId: 'A', applicability: true, result: 'typo' }], checkSetVersion: 'v1', requiredCheckIds: ['A'] }));
  await assert.rejects(host.call('consistency', 'inspect', { text: 'run 1' }, ctx), { code: 'SCHEMA_INVALID' });
  host.unregister('consistency');
  host.register(createConsistencyModule({ inspect: () => [{ checkId: 'A', applicability: true, result: 'passed' }, { checkId: 'A', applicability: true, result: 'passed' }], checkSetVersion: 'v1', requiredCheckIds: ['A'] }));
  await assert.rejects(host.call('consistency', 'inspect', { text: 'run 1' }, ctx), { code: 'SCHEMA_INVALID' });
  host.unregister('consistency'); installSimulationModules(host);
  result = await host.call('consistency', 'inspect', { text: 'include another.in\nrun 1' }, ctx);
  assert.equal(result.verdict, 'REVIEW'); assert.equal(result.checks.length, 10);
  assert.ok(result.checks.every(c => c.result === 'insufficient'));
});

test('real evidence, knowledge and gate backends keep identity and state authority', async t => {
  const f = await fixture(t); const { host, root } = await makeHost(t);
  const knowledge = await KnowledgeService.open({ file: join(root, 'knowledge.sqlite'), workspace: root, store: f.store, evidence: f.evidence });
  t.after(() => knowledge.close());
  const path = join(root, 'example.txt'); await writeFile(path, 'actual content\n');
  const meta = await knowledge.ingest(path, { domain: 'demo', type: 'case', title: 'example' });
  const source = { sourceId: meta.id, sourceKind: 'case', originGroupId: 'sample', locator: path };
  installSimulationModules(host, { knowledge, evidence: f.evidence, gate: f.gates,
    resolveEvidenceSource: async id => id === meta.id ? source : null });
  const reader = { ...ctx, actor: f.reviewer };
  assert.deepEqual((await host.call('knowledge', 'search', { query: 'actual', options: { domain: 'demo' } }, reader)).items, []);
  const receipt = await host.call('evidence', 'read', { sourceId: meta.id, scope: { startLine: 1, endLine: 1 } }, reader);
  assert.equal(receipt.reader.actorInstanceId, f.reviewer.actorInstanceId);
  const assessment = await host.call('evidence', 'assess', { readRefs: [receipt.id], policy: { localCaseRequired: true } }, reader);
  assert.equal(assessment.passed, true);
  const knowledgeReceipt = await host.call('knowledge', 'read', { sourceId: meta.id, scope: { startLine: 1, endLine: 1 } }, reader);
  assert.equal(knowledgeReceipt.sourceId, meta.id);
  await assert.rejects(host.call('evidence', 'read', { sourceId: 'bad', source: { locator: path } }, reader), { code: 'REFERENCE_MISSING' });
  await assert.rejects(host.call('gate', 'submit', { caseId: f.state.id, stageId: 'produce', subjectRefs: [], checks: [] }, reader), { code: 'SCHEMA_INVALID' });
  assert.equal(f.store.getCase(f.state.id).revision, 0);
  const artifact = await f.artifacts.publish(path);
  await f.kernel.command(f.state.id, 'start', { stageId: 'produce', producerInstanceId: f.writer.actorInstanceId });
  await f.kernel.command(f.state.id, 'produced', { stageId: 'produce', outputRefs: [artifact] });
  const request = { caseId: f.state.id, stageId: 'produce', subjectRefs: [artifact], producerInstanceId: f.writer.actorInstanceId,
    evidenceRefs: [receipt.id], policy: { localCaseRequired: true },
    checks: [{ checkId: 'CHECK-1', applicability: true, result: 'passed', observations: ['read actual file'], action: 'blocked' }] };
  const decision = await host.call('gate', 'submit', request, reader);
  assert.equal(decision.verdict, 'PASS');
  await f.kernel.command(f.state.id, 'review', { stageId: 'produce', receiptRef: decision.id });
  assert.equal(f.store.getCase(f.state.id).stages[0].status, 'passed');
  const forged = { ...reader, actor: { ...f.writer, capabilities: ['review.submit'] } };
  await assert.rejects(host.call('gate', 'submit', request, forged), { code: 'REVIEW_REQUIRED' });
});

test('audit detects modification, does not store inputs, and audit failure blocks writes', async t => {
  const { host, audit, root } = await makeHost(t);
  let writes = 0;
  host.register(createKnowledgeModule({ backend: { search: async () => ({ items: [] }), read: async () => { writes++; return { id: 'ok' }; } } }));
  const secretMarker = 'sensitive-probe-value-never-store';
  await host.call('knowledge', 'search', { query: secretMarker }, ctx);
  assert.equal((await readFile(join(root, 'audit.sqlite-wal'))).includes(Buffer.from(secretMarker)), false);
  audit.db.prepare('UPDATE module_audit SET event=? WHERE seq=1').run('{}');
  assert.equal(audit.verify().ok, false);
  await assert.rejects(host.call('knowledge', 'read', { sourceId: 'x' }, ctx), { code: 'AUDIT_UNAVAILABLE', sideEffectState: 'none' });
  assert.equal(writes, 0);
});

test('failure to write final audit yields unknown for a completed side effect', async () => {
  let count = 0, writes = 0;
  const host = new ModuleHost({ audit: { append() { if (++count === 2) throw new Error('disk'); } }, authorize: () => true });
  host.register(createKnowledgeModule({ backend: { search: async () => ({}), read: async () => { writes++; return { ok: true }; } } }));
  await assert.rejects(host.call('knowledge', 'read', { sourceId: 'x' }, ctx), { code: 'AUDIT_UNAVAILABLE', sideEffectState: 'unknown' });
  assert.equal(writes, 1);
});
