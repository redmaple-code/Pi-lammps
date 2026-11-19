import test from 'node:test';
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { fixture } from './fixtures.js';
import { id } from '../packages/contracts/index.js';
import { KnowledgeService } from '../packages/knowledge/index.js';
import { researchRoles, knowledgeRoles, conditionalRoles } from '../packages/application/support-roles.js';
import { recoverSolver } from '../packages/application/grinding-workflow.js';
import { simulationRoot } from '../scripts/build-packs.js';

function scriptedRoles(f, packets = {}) {
  const called = [];
  return { called, executeRole: async (task, operationId, signal, context) => {
    called.push(task.role);
    const actor = context.actor;
    for (const source of task.sources) await f.evidence.read(source, {}, actor);
    const packet = { decision: 'proceed', summary: 'Bounded evidence-based result for this specific case.',
      limitations: ['Single case, missing experimental and convergence validation'], ...(packets[task.role] || {}) };
    const ref = await f.artifacts.json(packet, { type: 'stage-packet', caseId: task.caseId, operationId });
    const receipt = { id: operationId, status: 'succeeded', actor, caseId: task.caseId, revisionId: task.revisionId,
      stageId: task.stageId, outputRefs: [ref] };
    f.store.put('role-operations', operationId, receipt);
    return receipt;
  } };
}

test('AI003 research branches execute the librarian and metadata-limited paper researcher', async t => {
  const f = await fixture(t);
  const runtime = scriptedRoles(f);
  const plan = { caseId: f.state.id, revisionId: f.state.revisionId, scheme: { scope: 'exploratory' } };
  const refs = await researchRoles(f, plan, runtime, simulationRoot, { attempt: 1 });
  assert.deepEqual(runtime.called, conditionalRoles.research);
  assert.equal(refs.length, 2);
  assert.ok(f.store.list('reads').some(read => read.sourceKind === 'metadata'));
});

test('AI003 knowledge branches curate and independently adjudicate a bounded real knowledge candidate', async t => {
  const f = await fixture(t);
  f.knowledge = await KnowledgeService.open({ file: join(f.root, 'knowledge.sqlite'), workspace: f.root, store: f.store, evidence: f.evidence });
  t.after(() => f.knowledge.close());
  const runtime = scriptedRoles(f, {
    'lammps-kb-curator': { lesson: 'The finite-output integration checks apply to this one exploratory run.', applicability: { scope: 'single-case' } },
    'lammps-kb-reviewer': { verdict: 'candidate', reasons: 'Scientific convergence and experimental agreement are unverified.' }
  });
  const plan = { caseId: f.state.id, revisionId: f.state.revisionId, scheme: { scope: 'exploratory' } };
  const analysisRef = await f.artifacts.json({ acceptance: { numerical: 'insufficient', model: 'insufficient' } }, { type: 'analysis', caseId: f.state.id });
  const result = await knowledgeRoles(f, plan, runtime, { analysisRef });
  assert.deepEqual(runtime.called, conditionalRoles.knowledge);
  assert.notEqual(result.proposal.curatorInstanceId, result.review.reviewer.actorInstanceId);
  assert.equal(result.review.verdict, 'candidate');
  const calls = runtime.called.length;
  await knowledgeRoles(f, plan, runtime, { analysisRef });
  assert.equal(runtime.called.length, calls);
});

test('AI002/003 recovered solver production reconciles the original operation without resubmitting', async t => {
  const f = await fixture(t, { stages: [{ id: 'execute', needs: [], produces: ['run'], reviewRequired: false },
    { id: 'analysis', needs: ['execute'], produces: ['analysis'], reviewRequired: false }] });
  await f.kernel.command(f.state.id, 'start', { stageId: 'execute', producerInstanceId: 'original-role', operationId: 'solver-original', request: { fixture: true } });
  f.store.claim('solver-original', { pid: process.pid });
  await f.kernel.command(f.state.id, 'fail', { stageId: 'execute', unknown: true, failureRef: 'interrupted-parent' });
  f.store.settle('solver-original', { status: 'succeeded', operationId: 'solver-original', exitCode: 0 });
  let submits = 0, collects = 0;
  f.executor = {
    submit: async () => { submits++; }, wait: async () => ({ status: 'succeeded', operationId: 'solver-original', exitCode: 0 }),
    collect: async () => { collects++; return { id: id('run'), operationId: 'solver-original', status: 'completed', outputRefs: [] }; }
  };
  // submit is idempotent in LocalExecutor. The stage adapter may query it but must preserve the identity.
  const runtime = scriptedRoles(f);
  const plan = { caseId: f.state.id, revisionId: f.state.revisionId, directory: f.root };
  await recoverSolver(f, plan, runtime, {});
  const stage = f.store.getCase(f.state.id).stages[0];
  assert.equal(stage.status, 'passed');
  assert.equal(stage.operationId, 'solver-original');
  assert.equal(stage.attempt, 1);
  assert.equal(stage.recoveredProducerInstanceId, 'original-role');
  assert.equal(f.store.operations(f.state.id).length, 1);
  assert.equal(collects, 1);
  assert.equal(submits, 1);
  await recoverSolver(f, plan, runtime, {});
  assert.equal(collects, 1);
});
