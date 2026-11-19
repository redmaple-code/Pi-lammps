import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openApplication } from '../packages/application/index.js';
import { createGrindingPlan, runGrinding } from '../packages/application/grinding.js';
import { id } from '../packages/contracts/index.js';
import { simulationRoot } from '../scripts/build-packs.js';

test('AI003 confirmed input failure is diagnosed, revised, independently reviewed and re-executed with history', async t => {
  const root = await mkdtemp(join(tmpdir(), 'redmaple-role-failure-'));
  const app = await openApplication({ workspace: root, stateDirectory: join(root, 'state') });
  t.after(() => app.close());
  const parameters = { ...JSON.parse(await readFile(join(simulationRoot, 'domains/lammps/grinding.parameters.json'), 'utf8')),
    cells: [20, 10, 8], equilibrationSteps: 100, approachSteps: 100, grindingSteps: 200 };
  const parametersFile = join(root, 'parameters.json');
  await writeFile(parametersFile, JSON.stringify(parameters));
  const plan = await createGrindingPlan(app, { python: 'C:/Users/redmaple/AppData/Local/Programs/Python/Python311/python.exe',
    lammps: process.execPath, parametersFile, validation: true });
  let submissions = 0;
  app.executor = {
    directory: operationId => join(root, 'fake-jobs', operationId),
    submit: async (request, operationId) => {
      const operation = app.store.operation(operationId);
      if (operation.status !== 'intent') return operation.receipt;
      submissions++;
      app.store.claim(operationId, { pid: process.pid });
      const directory = join(root, 'fake-jobs', operationId, 'work');
      await mkdir(directory, { recursive: true });
      await writeFile(join(directory, 'log.lammps'), submissions === 1 ? 'ERROR: Illegal command (injected fixture)\n' : 'REDMAPLE_GRINDING_COMPLETE\n');
      return app.store.settle(operationId, { status: submissions === 1 ? 'failed' : 'succeeded', operationId, exitCode: submissions === 1 ? 1 : 0 }).receipt;
    },
    wait: async operationId => app.store.operation(operationId).receipt,
    collect: async operationId => ({ id: id('fixture-run'), operationId, status: 'completed', outputRefs: [] })
  };
  const calls = [];
  const runtime = { active: new Map(), executeRole: async (task, operationId, signal, context = {}) => {
    calls.push({ role: task.role, stageId: task.stageId });
    const actor = context.actor || { actorId: task.role, actorInstanceId: id('mock-role'), kind: 'agent', capabilities: ['review.submit'] };
    const reads = [];
    for (const source of task.sources || []) reads.push(await app.evidence.read(source, {}, actor));
    if (task.role === 'lammps-reviewer') {
      const stage = app.store.getCase(plan.caseId).stages.find(item => item.id === task.stageId);
      const bindings = stage.evidencePolicy.claims.map(claim => {
        const read = reads.find(item => claim.sourceIds.includes(item.sourceId));
        const quote = read.excerpt.split(/\r?\n/).find(line => line.trim().length >= 12);
        return { claimId: claim.id, readRef: read.id, quote, judgment: 'supports', reason: 'Fixture checks the registered source and exact quoted content.' };
      });
      const request = { caseId: plan.caseId, stageId: stage.id, subjectRefs: stage.outputRefs,
        producerInstanceId: stage.producerInstanceId, evidenceRefs: reads.map(read => read.id), checks: [...task.hostChecks,
          { checkId: stage.id, applicability: true, result: 'passed', action: 'revise', observations: ['Mock independent review'], extensions: { claimEvidence: bindings } }] };
      if (stage.id === 'wf-02') {
        const wrong = structuredClone(request);
        wrong.checks.at(-1).extensions.claimEvidence[1].quote = 'invented quotation';
        assert.equal((await app.gates.submit(wrong, actor)).verdict, 'BLOCKED');
      }
      await app.gates.submit(request, actor);
    } else if (context.tools?.length) {
      if (task.stageId === 'wf-03a') {
        const path = join(plan.directory, 'inputs/in.grinding');
        const before = await readFile(path, 'utf8');
        const invalid = await context.tools[0].execute('invalid-input', { inputText: '{}' });
        assert.ok(invalid.details.checks.some(check => check.result === 'failed'));
        assert.equal(await readFile(path, 'utf8'), before, 'Invalid candidate overwrote the generated input');
      }
      await context.tools[0].execute('fixture-call', {});
    }
    const packet = { decision: 'proceed', summary: 'Fixture confirms the bounded stage result.', limitations: ['Deterministic role fixture'],
      ...(task.stageId.startsWith('failure:') ? { earliestStage: 'wf-03a' } : {}) };
    const ref = await app.artifacts.json(packet, { type: 'stage-packet', caseId: task.caseId, operationId });
    const receipt = { id: operationId, actor, status: 'succeeded', outputRefs: [ref], evidenceRefs: reads.map(read => read.id),
      caseId: task.caseId, revisionId: task.revisionId, stageId: task.stageId };
    app.store.put('role-operations', operationId, receipt);
    return receipt;
  } };
  await runGrinding(app, plan, { runtime, stopAfterStage: 'execute' });
  const operations = app.store.operations(plan.caseId).filter(operation => operation.stageId === 'execute');
  assert.deepEqual(operations.map(operation => operation.status), ['failed', 'succeeded']);
  assert.equal(submissions, 2);
  assert.ok(calls.some(call => call.role === 'lammps-analyst' && call.stageId.startsWith('failure:')));
  assert.ok(calls.some(call => call.role === 'lammps-simulation-reasoner' && call.stageId.startsWith('failure:')));
  const inputStage = app.store.getCase(plan.caseId).stages.find(stage => stage.id === 'wf-03a');
  assert.equal(inputStage.attempt, 2);
  assert.equal(app.store.get('reviews', inputStage.receipts.at(-1)).verdict, 'PASS');
  assert.ok(app.store.getCase(plan.caseId).invalidations.some(item => item.affected.includes('execute')));
  assert.deepEqual(app.store.list('reviews').filter(review => review.stageId === 'wf-02').map(review => review.verdict), ['BLOCKED', 'PASS']);
  const completedCalls = calls.length;
  await runGrinding(app, plan, { runtime, stopAfterStage: 'execute' });
  assert.equal(calls.length, completedCalls, 'An already passed stop stage must not start analysis during recovery');
  assert.equal(submissions, 2, 'Recovery stop duplicated a solver submission');
});
