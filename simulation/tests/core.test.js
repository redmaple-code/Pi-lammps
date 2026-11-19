import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixtures.js';
import { canonical, digest, evaluateCondition, topological, id } from '../packages/contracts/index.js';
import { parseDocumentText, resolvePack, validateGraph } from '../packages/pack-resolver/index.js';
import { recordAuthorization } from '../packages/kernel/authorization.js';
import { buildPacks, simulationRoot } from '../scripts/build-packs.js';

test('canonical identities, strict YAML and three-valued conditions preserve unknown', () => {
  assert.equal(digest({ a: 1, b: 2 }), digest({ b: 2, a: 1 }));
  assert.throws(() => canonical({ x: NaN }), { code: 'SCHEMA_INVALID' });
  assert.throws(() => parseDocumentText('x: 1\nx: 2'), { code: 'SCHEMA_INVALID' });
  assert.equal(evaluateCondition({ all: [{ fact: 'missing', op: 'eq', value: 1 }, { fact: 'yes', op: 'eq', value: true }] }, { yes: true }), 'unknown');
  assert.equal(evaluateCondition({ not: { fact: 'missing', op: 'eq', value: true } }, {}), 'unknown');
  assert.throws(() => topological([{ id: 'a', needs: ['b'] }, { id: 'b', needs: ['a'] }]), { code: 'SCHEMA_INVALID' });
});
test('pack resolution locks bytes and distinguishes draft validation from production', async () => {
  await buildPacks();
  const registry = { 'local-lammps': { version: '0.1.0', capabilities: ['molecular.dynamics.solve'] } };
  const path = join(simulationRoot, 'domains/lammps/manifest.json');
  const validation = await resolvePack(path, { validation: true, capabilityRegistry: registry });
  assert.equal(validation.executionReady, true);
  const production = await resolvePack(path, { capabilityRegistry: registry });
  assert.equal(production.executionReady, false);
  assert.ok(production.diagnostics.some(item => item.code === 'DRAFT_PACKAGE'));
  for (const name of ['molecular-dynamics', 'structural-mechanics', 'discrete-event']) {
    const result = await resolvePack(join(simulationRoot, `domains/examples/${name}.json`));
    assert.equal(result.structurallyValid, true);
    assert.equal(result.executionReady, false);
  }
  assert.throws(() => validateGraph({ inputs: [], outputs: [], stages: [{ id: 'a', needs: [], consumes: ['unproduced'], produces: [] }] }), { code: 'REFERENCE_MISSING' });
});
test('binary publication and immutable closure detect changed content', async t => {
  const f = await fixture(t);
  const file = join(f.root, 'restart');
  await writeFile(file, Buffer.from([0, 255, 128, 64]));
  const ref = await f.artifacts.publish(file, { type: 'restart' });
  assert.deepEqual(await f.artifacts.read(ref), Buffer.from([0, 255, 128, 64]));
  const closure = await f.artifacts.closure([{ path: 'input/restart', ref }]);
  await f.artifacts.materialize(closure, join(f.root, 'run'));
  assert.deepEqual(await readFile(join(f.root, 'run/input/restart')), Buffer.from([0, 255, 128, 64]));
  await writeFile(ref.locator, Buffer.from([1]));
  await assert.rejects(f.artifacts.verify(ref), { code: 'ARTIFACT_CHANGED' });
});
test('CAS, command deduplication and operation reconciliation never blindly replay', async t => {
  const f = await fixture(t);
  const options = { commandId: 'first', expectedRevision: 0 };
  const first = await f.kernel.command(f.state.id, 'start', { stageId: 'produce', producerInstanceId: f.writer.actorInstanceId }, options);
  const repeated = await f.kernel.command(f.state.id, 'start', { stageId: 'produce', producerInstanceId: f.writer.actorInstanceId }, options);
  assert.equal(first.state.revision, repeated.state.revision);
  await assert.rejects(f.kernel.command(f.state.id, 'start', { stageId: 'solve' }, { expectedRevision: 0 }), { code: 'STATE_CONFLICT' });
  const state = f.store.getCase(f.state.id);
  f.store.dispatch(state.id, { id: 'intent' }, state.revision, () => ({ intents: [{ operationId: 'op', request: { tool: 'x' } }] }));
  f.store.claim('op', { pid: process.pid });
  assert.throws(() => f.store.claim('op', { pid: process.pid }), { code: 'RESULT_UNKNOWN' });
  f.store.settle('op', { status: 'succeeded', value: 1 });
  assert.equal(f.store.settle('op', { status: 'succeeded', value: 1 }).receipt.value, 1);
  assert.throws(() => f.store.settle('op', { status: 'failed' }), { code: 'RECEIPT_CONFLICT' });
});
test('independent review, evidence origin and three successive revisions remain enforced', async t => {
  const f = await fixture(t);
  const file = join(f.root, 'case.in'); await writeFile(file, 'case actual input\n');
  const ref = await f.artifacts.publish(file);
  const source = { sourceId: 'case', locator: file, sourceKind: 'case', originGroupId: 'original' };
  const read = await f.evidence.read(source, { representation: 'fullText' }, f.reviewer);
  const manual = await f.evidence.read({ ...source, sourceId: 'manual', sourceKind: 'manual' }, { representation: 'fullText' }, f.reviewer);
  assert.equal((await f.evidence.assess([read.id, manual.id], { dualEvidence: true })).passed, false);
  for (let attempt = 0; attempt < 3; attempt++) {
    await f.kernel.command(f.state.id, 'start', { stageId: 'produce', producerInstanceId: f.writer.actorInstanceId });
    await f.kernel.command(f.state.id, 'produced', { stageId: 'produce', outputRefs: [ref] });
    await assert.rejects(f.gates.submit({ caseId: f.state.id, stageId: 'produce', subjectRefs: [ref], producerInstanceId: f.writer.actorInstanceId,
      checks: [] }, { ...f.writer, capabilities: ['review.submit'] }), { code: 'REVIEW_REQUIRED' });
    const receipt = await f.gates.submit({ caseId: f.state.id, stageId: 'produce', subjectRefs: [ref], producerInstanceId: f.writer.actorInstanceId,
      evidenceRefs: [read.id], checks: [{ checkId: 'CHECK-1', applicability: true, result: 'failed', observations: ['bounded correction'], action: 'revise' }] }, f.reviewer);
    await f.kernel.command(f.state.id, 'review', { stageId: 'produce', receiptRef: receipt.id });
  }
  assert.equal(f.store.getCase(f.state.id).stages[0].status, 'blocked');
  await assert.rejects(f.kernel.command(f.state.id, 'start', { stageId: 'produce' }), { code: 'REVISION_LIMIT' });
});

test('review binds producer, all subject artifacts and the frozen evidence policy', async t => {
  const f = await fixture(t, { stages: [{ id: 'produce', needs: [], reviewRequired: true,
    requiredCheckIds: ['CHECK-1'], evidencePolicy: { localCaseRequired: true } }] });
  const ref = await f.artifacts.text('candidate'), other = await f.artifacts.text('another candidate');
  await f.kernel.command(f.state.id, 'start', { stageId: 'produce', producerInstanceId: f.writer.actorInstanceId });
  await f.kernel.command(f.state.id, 'produced', { stageId: 'produce', outputRefs: [ref] });
  const request = { caseId: f.state.id, stageId: 'produce', subjectRefs: [ref], producerInstanceId: f.writer.actorInstanceId,
    policy: { localCaseRequired: false }, checks: [{ checkId: 'CHECK-1', applicability: true, result: 'passed', observations: ['Checked actual candidate'], action: 'blocked' }] };
  await assert.rejects(f.gates.submit({ ...request, producerInstanceId: 'invented-producer' }, { ...f.writer, capabilities: ['review.submit'] }), { code: 'REVIEW_REQUIRED' });
  await assert.rejects(f.gates.submit({ ...request, subjectRefs: [other] }, f.reviewer), { code: 'ARTIFACT_CHANGED' });
  assert.equal((await f.gates.submit(request, f.reviewer)).verdict, 'BLOCKED');
});
