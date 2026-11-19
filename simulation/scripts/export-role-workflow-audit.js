import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { simulationRoot } from './build-packs.js';
import { hashFile } from '../packages/artifacts/index.js';
import { digest, utc } from '../packages/contracts/index.js';
import { openApplication } from '../packages/application/index.js';
import { currentGrindingResult } from '../packages/application/result-state.js';
import { deliveryStatus, improvementScope } from '../packages/application/delivery-status.js';

const directory = join(simulationRoot, 'audits/2026-11-18-role-workflow');
const sourceFiles = [
  'apps/cli/main.js', 'runtime.example.json', 'packages/runtime-redmaple/index.js',
  'packages/application/grinding.js', 'packages/application/grinding-workflow.js', 'packages/application/grinding-stages.js',
  'packages/application/result-state.js', 'packages/application/workflow.js', 'packages/application/partial.js',
  'packages/application/support-roles.js', 'packages/application/delivery-status.js',
  'packages/kernel/index.js', 'packages/kernel/state.js', 'packages/kernel/gates.js', 'packages/kernel/evidence.js',
  'packages/execution/index.js', 'packages/execution/worker.js', 'tests/execution.test.js',
  'packages/analysis/scientific-validation.js', 'packages/presentation/index.js',
  'tests/grinding-recovery.test.js', 'tests/support-roles.test.js', 'tests/role-failure.test.js',
  'scripts/verify-role-grinding.js', 'scripts/export-role-workflow-audit.js', 'domains/lammps/references/fix-print-command.corrected.txt',
  'packages/runtime-redmaple/curl-fetch.js', 'packages/runtime-redmaple/configuration.js',
  'tests/curl-fetch.test.js', 'tests/provider-configuration.test.js', '../redmaple-agent/src/session-controls.js',
  '../redmaple-agent/src/compaction.js', '../redmaple-agent/src/native-compaction.js', '../redmaple-agent/src/permissions.js',
  '../redmaple-agent/test/agent.test.js', '../redmaple-agent/test/full-runtime.test.js'
];
const sources = [];
for (const path of sourceFiles) sources.push({ path, byteSha256: await hashFile(join(simulationRoot, path)) });
const sourceDigest = digest(sources);
if (process.argv.includes('--prepare')) {
  await writeFile(join(directory, 'verification-inputs.json'), JSON.stringify({ sourceDigest, sources, preparedAt: utc() }, null, 2));
  console.log(JSON.stringify({ prepared: true, sourceDigest, files: sources.length }));
} else {
  const inputs = JSON.parse(await readFile(join(directory, 'verification-inputs.json'), 'utf8'));
  assert.equal(sourceDigest, inputs.sourceDigest, 'Source changed after verification preparation');
  const regressionPath = join(simulationRoot, 'audits/2026-11-18-regression-final.log');
  const regression = await readFile(regressionPath, 'utf8');
  const failures = Number(regression.match(/^# fail (\d+)$/m)?.[1]);
  const passed = Number(regression.match(/^# pass (\d+)$/m)?.[1]);
  assert.equal(failures, 0);
  assert.ok(passed > 0 && regression.includes('AI004 delivery cannot close'));
  const details = JSON.parse(await readFile(join(directory, 'case.json'), 'utf8'));
  const app = await openApplication({ workspace: simulationRoot, stateDirectory: details.stateDirectory });
  try {
    const state = app.store.getCase(details.caseId);
    assert.equal(state.status, 'completed');
    const result = await currentGrindingResult(app, details.caseId);
    assert.equal(result.status, 'completed');
    const solverOperations = app.store.operations(details.caseId).filter(operation => operation.stageId === 'execute');
    assert.equal(solverOperations.length, 1);
    assert.equal(solverOperations[0].status, 'succeeded');
    const plan = app.store.get('grinding-plans', details.caseId);
    const recovery = JSON.parse(await readFile(join(directory, 'recovery.json'), 'utf8'));
    assert.equal(recovery.solverDuplicated, false);
    const redraw = JSON.parse(await readFile(join(directory, 'redraw-recovery.json'), 'utf8'));
    assert.equal(redraw.oldCompletedResultRejected, true);
    assert.equal(redraw.solverOperationsAfter, 1);
    const roles = app.store.list('role-operations').filter(operation => operation.caseId === details.caseId);
    const successful = roles.filter(operation => operation.status === 'succeeded');
    assert.ok(successful.length > 8);
    assert.ok(successful.every(operation => operation.provider === 'aispot-ds' && operation.model === 'deepseek-v4.1-flash'));
    const reviews = state.stages.filter(stage => stage.reviewRequired).map(stage => app.store.get('reviews', stage.receipts.at(-1)));
    assert.ok(reviews.every(review => review.verdict === 'PASS' && review.reviewerInstanceId !== review.producerInstanceId
      && review.evidence.claims.every(claim => claim.supported)));
    assert.equal(result.measured.motionAudit.passed, true);
    assert.equal(result.acceptance.numerical, 'insufficient');
    assert.equal(result.acceptance.model, 'insufficient');
    assert.equal(result.acceptance.task, 'cannotCompute');
    assert.ok(result.figures.every(figure => figure.inspection.opened && figure.inspection.checked.length));
    const evidenceRefs = [
      { path: 'audits/2026-11-18-regression-final.log', byteSha256: await hashFile(regressionPath) },
      { path: 'audits/2026-11-18-role-workflow/recovery.json', byteSha256: await hashFile(join(directory, 'recovery.json')) },
      { path: 'audits/2026-11-18-role-workflow/redraw-recovery.json', byteSha256: await hashFile(join(directory, 'redraw-recovery.json')) }
    ];
    const receipts = improvementScope.map(issueId => ({ issueId, verdict: 'passed', executedAt: utc(), sourceDigest, evidenceRefs,
      limitations: issueId === 'AI003' ? ['Knowledge and solver-failure branches use deterministic role fixtures with real local services; normal production stages use live DS sessions']
        : issueId === 'AI007' ? ['Single case; convergence, quantitative model validation and matched experiment comparison remain insufficient'] : [] }));
    const delivery = deliveryStatus(receipts, { currentSourceDigest: sourceDigest });
    const budget = app.store.get('model-budgets', `${details.caseId}:${state.revisionId}`);
    const handoff = { revision: 'ROLE-2', date: '2026-11-18', scope: improvementScope, caseId: details.caseId,
      workflowStatus: state.status, provider: 'aispot-ds', model: 'deepseek-v4.1-flash',
      sourceDigest, sources, regression: { passed, failures }, delivery, receipts,
      solverOperationId: solverOperations[0].operationId, solverOperations: 1, roleOperations: roles.length,
      succeededRoleOperations: successful.length, activeRolesVerified: [...new Set(successful.map(operation => operation.actor.actorId))],
      preservedUnsuccessfulRoles: roles.filter(operation => operation.status !== 'succeeded').map(({ id, status, error }) => ({ id, status, error })),
      budget, scientificAcceptance: result.acceptance, motionAudit: result.measured.motionAudit,
      diagnostics: result.measured.diagnostics, planPath: join(plan.directory, 'PLAN.json'), reportPath: result.reportPath,
      reviewEvidence: reviews.map(review => ({ stageId: review.stageId, reviewId: review.id,
        policyDigest: review.evidencePolicyDigest, reads: review.evidence.readRefs.map(ref => {
          const read = app.store.get('reads', ref);
          return { id: read.id, sourceId: read.sourceId, digest: read.digest, locator: read.locator,
            startLine: read.startLine, endLine: read.endLine };
        }) })),
      figures: result.figures, knowledgeBranch: 'fixture verified', failureRepairBranch: 'fixture verified', generatedAt: utc() };
    await writeFile(join(directory, 'handoff.json'), JSON.stringify(handoff, null, 2));
    await writeFile(join(directory, 'final-result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ delivery: delivery.status, verified: delivery.verified, passed, failures,
      solverOperations: 1, caseId: details.caseId, roleOperations: roles.length, modelCalls: budget.used }));
  } finally { app.close(); }
}
