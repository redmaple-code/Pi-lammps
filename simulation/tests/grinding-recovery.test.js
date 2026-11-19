import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixtures.js';
import { id, digest, serviceActor } from '../packages/contracts/index.js';
import { WorkflowRunner } from '../packages/application/workflow.js';
import { currentGrindingResult, saveGrindingResult } from '../packages/application/result-state.js';
import { freezeProtocol, scientificValidation } from '../packages/analysis/scientific-validation.js';
import { exploratoryProtocol, stageEvidencePolicy } from '../packages/application/grinding-workflow.js';
import { RedMapleRuntimePort } from '../packages/runtime-redmaple/index.js';
import { deliveryStatus, improvementScope } from '../packages/application/delivery-status.js';

async function finish(f, stageId, ref) {
  await f.kernel.command(f.state.id, 'start', { stageId, producerInstanceId: f.writer.actorInstanceId });
  const receipt = { id: id('completion') };
  f.store.put('completions', receipt.id, receipt, 0);
  return f.kernel.command(f.state.id, 'produced', { stageId, outputRefs: [ref], outputSlots: { value: [ref] }, completionReceiptRef: receipt.id });
}

test('AI002 completed cache is rejected after invalidation even with the same science revision', async t => {
  const f = await fixture(t, { stages: [{ id: 'wf-05', needs: [], reviewRequired: false, produces: ['value'] }] });
  const ref = await f.artifacts.text('image manifest');
  await finish(f, 'wf-05', ref);
  const saved = saveGrindingResult(f, { caseId: f.state.id, status: 'completed', figures: [] });
  assert.equal((await currentGrindingResult(f, f.state.id)).stateBinding, saved.stateBinding);
  await f.kernel.command(f.state.id, 'invalidate', { changedStages: ['wf-05'], reason: 'style' });
  assert.equal(f.store.getCase(f.state.id).revisionId, saved.revisionId);
  assert.deepEqual(f.store.getCase(f.state.id).stages[0].outputSlots, {});
  assert.equal(await currentGrindingResult(f, f.state.id), null);
  await finish(f, 'wf-05', ref);
  assert.equal(await currentGrindingResult(f, f.state.id), null);
});

test('AI002 completed result checks actual bytes and legacy snapshots need reconstruction', async t => {
  const f = await fixture(t, { stages: [{ id: 'wf-05', needs: [], reviewRequired: false }] });
  const ref = await f.artifacts.text('original');
  await finish(f, 'wf-05', ref);
  f.store.put('grinding-results', f.state.id, { caseId: f.state.id, revisionId: f.state.revisionId, status: 'completed' }, 0);
  assert.equal(await currentGrindingResult(f, f.state.id), null);
  saveGrindingResult(f, { caseId: f.state.id, status: 'completed', figures: [] });
  await writeFile(ref.locator, 'changed');
  await assert.rejects(currentGrindingResult(f, f.state.id), { code: 'ARTIFACT_CHANGED' });
});

test('AI002 image inspection is a durable stop and resume does not regenerate images', async t => {
  const f = await fixture(t, { stages: [{ id: 'wf-05', needs: [], reviewRequired: false, produces: ['images'] }] });
  const ref = await f.artifacts.text('image');
  await f.kernel.command(f.state.id, 'start', { stageId: 'wf-05', producerInstanceId: f.writer.actorInstanceId });
  await f.kernel.command(f.state.id, 'awaitInspection', { stageId: 'wf-05', outputRefs: [ref] });
  saveGrindingResult(f, { caseId: f.state.id, status: 'awaitingImageInspection', figures: [] });
  assert.equal((await currentGrindingResult(f, f.state.id)).status, 'awaitingImageInspection');
  const runner = new WorkflowRunner({ app: f, handlers: {} });
  assert.equal((await runner.run(f.state.id)).stages[0].status, 'awaitingInspection');
});

test('AI003 stage/role dispatch overrides a shared capability and uses host producer identity', async t => {
  const f = await fixture(t, { stages: [{ id: 'design', role: 'architect', capability: 'solve', needs: [], consumes: [], produces: ['design'], reviewRequired: false }] });
  const actor = { actorId: 'architect', actorInstanceId: 'real-role-instance', kind: 'agent' };
  let selected;
  const runner = new WorkflowRunner({ app: f, handlers: {
    solve: { actor: f.writer, execute: () => { throw Error('wrong dispatch'); } },
    architect: { createActor: () => actor, execute: async context => {
      selected = context.actor;
      return { outputSlots: { design: [await f.artifacts.text('design')] } };
    } }
  } });
  const state = await runner.run(f.state.id);
  assert.equal(state.status, 'completed');
  assert.equal(selected.actorInstanceId, state.stages[0].producerInstanceId);
});

test('AI003 confirmed failure can resume and passed predecessors are not repeated', async t => {
  const f = await fixture(t, { stages: [{ id: 'first', role: 'builder', needs: [], consumes: [], produces: ['a'], reviewRequired: false },
    { id: 'second', role: 'worker', needs: ['first'], consumes: ['a'], produces: ['b'], reviewRequired: false }] });
  let firstCalls = 0, fail = true;
  const runner = new WorkflowRunner({ app: f, handlers: {
    builder: { actor: f.writer, execute: async () => { firstCalls++; return { outputSlots: { a: [await f.artifacts.text('a')] } }; } },
    worker: { actor: f.writer, execute: async () => { if (fail) throw Error('injected'); return { outputSlots: { b: [await f.artifacts.text('b')] } }; } }
  } });
  await assert.rejects(runner.run(f.state.id), /injected/);
  fail = false;
  assert.equal((await runner.run(f.state.id)).status, 'completed');
  assert.equal(firstCalls, 1);
});

test('AI006 source category and actual reading cannot satisfy an unrelated claim', async t => {
  const f = await fixture(t);
  const path = join(f.root, 'source.txt');
  await writeFile(path, 'Exact long source quotation about spherical indentation.');
  const actor = f.reviewer;
  const read = await f.evidence.read({ sourceId: 'indent-manual', locator: path, sourceKind: 'manual', originGroupId: 'manual' }, {}, actor);
  const policy = { claims: [{ id: 'potential-fit', sourceIds: ['potential-paper'], blocking: true }] };
  const binding = { claimId: 'potential-fit', readRef: read.id, quote: 'source quotation about spherical indentation', judgment: 'supports', reason: 'This is a claimed applicability explanation.' };
  assert.equal((await f.evidence.assess([read.id], policy, actor.actorInstanceId, [binding])).passed, false);
  policy.claims[0].sourceIds = ['indent-manual'];
  assert.equal((await f.evidence.assess([read.id], policy, actor.actorInstanceId, [binding])).passed, true);
  policy.claims[0].sourceDigests = ['sha256:wrong-source-version'];
  assert.equal((await f.evidence.assess([read.id], policy, actor.actorInstanceId, [binding])).passed, false);
  policy.claims[0].sourceDigests = [read.digest];
  binding.quote = 'invented quotation that does not exist';
  assert.equal((await f.evidence.assess([read.id], policy, actor.actorInstanceId, [binding])).passed, false);
});

test('AI006 required host checks survive model review and duplicate checks are rejected', async t => {
  const f = await fixture(t, { stages: [{ id: 'input', needs: [], reviewRequired: true, requiredCheckIds: ['HOST-input', 'input'] }] });
  const ref = await f.artifacts.text('input');
  await f.kernel.command(f.state.id, 'start', { stageId: 'input', producerInstanceId: f.writer.actorInstanceId });
  await f.kernel.command(f.state.id, 'produced', { stageId: 'input', outputRefs: [ref] });
  const stage = f.store.getCase(f.state.id).stages[0];
  const runtime = new RedMapleRuntimePort({ ...f, workspace: f.root, roles: {}, options: {} });
  const task = { caseId: f.state.id, stageId: 'input', reviewBinding: digest({ revisionId: f.state.revisionId, attempt: stage.attempt, subjectRefs: [ref], policy: {} }),
    hostChecks: [{ checkId: 'HOST-input', applicability: true, result: 'failed', observations: ['Wrong units'], action: 'blocked' }] };
  const tool = runtime.createServiceTools({ actor: f.reviewer, task, operationId: 'review' }).find(item => item.name === 'simulation_review_submit');
  const check = { checkId: 'input', applicability: true, result: 'passed', observations: ['Model says pass'], action: 'revise' };
  const blocked = (await tool.execute('call', { checks: [check], evidenceRefs: [] })).details;
  assert.equal(blocked.verdict, 'BLOCKED');
  await f.kernel.command(f.state.id, 'review', { stageId: 'input', receiptRef: blocked.id });
  assert.equal(f.store.getCase(f.state.id).stages[0].status, 'blocked');
  assert.equal((await new WorkflowRunner({ app: f, handlers: {} }).run(f.state.id)).stages[0].status, 'blocked');
  await assert.rejects(tool.execute('call', { checks: [{ ...check, checkId: 'HOST-input' }], evidenceRefs: [] }), { code: 'SCHEMA_INVALID' });
  await f.kernel.command(f.state.id, 'invalidate', { changedStages: ['input'], reason: 'changed' });
  await assert.rejects(tool.execute('call', { checks: [check], evidenceRefs: [] }), { code: 'ARTIFACT_CHANGED' });
});

test('AI007 exploratory protocol produces explicit missing scientific comparisons', () => {
  const protocol = exploratoryProtocol();
  const result = scientificValidation(protocol, { meanNormalForceNn: 20 }, { dataRefs: [], runRef: 'run', parameters: {} });
  assert.equal(result.numerical, 'insufficient');
  assert.equal(result.model, 'insufficient');
  assert.equal(result.task, 'insufficient');
  assert.ok(result.comparisons.every(item => item.verdict === 'cannotCompute' && item.limitations.length));
});

test('AI007 finite references enforce tolerances, matching conditions and immutable protocol', () => {
  const protocol = freezeProtocol({ mode: 'exploratory', comparisons: [{ id: 'normal', dimension: 'task', field: 'fz', unit: 'nN',
    methodRef: 'mean/1', reference: 'matched reference', referenceRefs: ['source'], referenceValue: 10, absoluteTolerance: 1,
    borderlineTolerance: .01, conditions: { temperatureK: 300 }, selection: 'second half', sampling: { everySteps: 10 } }] });
  const compare = (fz, temperatureK = 300) => scientificValidation(protocol, { fz }, { dataRefs: [], runRef: 'r', parameters: { temperatureK } });
  assert.equal(compare(10).task, 'passed');
  assert.equal(compare(12).task, 'failed');
  assert.equal(compare(11).comparisons[0].verdict, 'borderline');
  assert.equal(compare(10, 500).comparisons[0].verdict, 'cannotCompute');
  assert.equal(compare(undefined).comparisons[0].verdict, 'cannotCompute');
  assert.equal(scientificValidation(protocol, { fz: 10 }, { dataRefs: [], runRef: 'r', parameters: { temperatureK: 300 },
    measurementUnits: { fz: 'N' } }).comparisons[0].verdict, 'cannotCompute');
  protocol.comparisons[0].absoluteTolerance = 100;
  assert.throws(() => compare(10), { code: 'ARTIFACT_CHANGED' });
  assert.equal(stageEvidencePolicy('wf-02').claims.length, 2);
});

test('AI004 delivery cannot close with missing, failed or stale verification evidence', () => {
  const receipts = improvementScope.map(issueId => ({ issueId, verdict: 'passed', executedAt: '2026-11-18', evidenceRefs: ['test-receipt'], sourceDigest: 'current' }));
  assert.equal(deliveryStatus(receipts, { currentSourceDigest: 'current' }).status, 'verified');
  assert.equal(deliveryStatus(receipts.slice(1), { currentSourceDigest: 'current' }).status, 'partial');
  assert.equal(deliveryStatus([...receipts, { ...receipts[0], verdict: 'failed' }], { currentSourceDigest: 'current' }).status, 'partial');
  assert.equal(deliveryStatus(receipts, { currentSourceDigest: 'changed' }).verified, 0);
  assert.equal(improvementScope.includes('AI005'), false);
});

test('AI003 publication contract rejects JSON null and incorrect artifact types before writing', async t => {
  const f = await fixture(t);
  const runtime = new RedMapleRuntimePort({ ...f, workspace: f.root, roles: {}, options: {} });
  const tool = runtime.createServiceTools({ actor: { ...f.writer, capabilities: ['artifact.write'] },
    task: { publicationType: 'stage-packet' }, operationId: 'publication-test' }).find(item => item.name === 'simulation_artifact_publish');
  await assert.rejects(tool.execute('call', { type: 'stage-packet', name: 'null.json', content: 'null' }), { code: 'SCHEMA_INVALID' });
  await assert.rejects(tool.execute('call', { type: 'application/json', name: 'wrong.json', content: '{}' }), { code: 'SCHEMA_INVALID' });
  assert.equal(f.store.list('artifacts').length, 0);
});

test('AI003 analyst reads outputs through their owning solver operation and rejects foreign or binary artifacts', async t => {
  const f = await fixture(t);
  const state = f.store.getCase(f.state.id);
  f.store.dispatch(state.id, { id: 'output-owner' }, state.revision, () => ({ intents: [{ operationId: 'owned-solver', stageId: 'solve', request: { fixture: true } }] }));
  const output = await f.artifacts.text('step force\n1 2\n', { type: 'file', operationId: 'owned-solver', name: 'grinding.csv' });
  const foreign = await f.artifacts.text('foreign', { caseId: 'another-case' });
  const binary = await f.artifacts.text('binary fixture', { type: 'file', operationId: 'owned-solver', name: 'final.restart' });
  const runtime = new RedMapleRuntimePort({ ...f, workspace: f.root, roles: {}, options: {} });
  const tool = runtime.createServiceTools({ actor: { ...f.writer, capabilities: [] }, task: { caseId: state.id }, operationId: 'read-outputs' })
    .find(item => item.name === 'simulation_artifact_read');
  assert.ok((await tool.execute('read', { artifactId: output.artifactId })).details.excerpt.includes('1 2'));
  await assert.rejects(tool.execute('foreign', { artifactId: foreign.artifactId }), { code: 'PERMISSION_DENIED' });
  await assert.rejects(tool.execute('binary', { artifactId: binary.artifactId }), { code: 'PERMISSION_DENIED' });
});
