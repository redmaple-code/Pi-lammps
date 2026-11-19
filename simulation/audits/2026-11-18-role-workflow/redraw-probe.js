import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { openApplication } from '../../packages/application/index.js';
import { runGrinding } from '../../packages/application/grinding.js';
import { currentGrindingResult } from '../../packages/application/result-state.js';

const root = process.cwd();
const directory = join(root, 'audits/2026-11-18-role-workflow');
const details = JSON.parse(await readFile(join(directory, 'case.json'), 'utf8'));
const app = await openApplication({ workspace: root, stateDirectory: details.stateDirectory });
try {
  const prior = await currentGrindingResult(app, details.caseId);
  assert.equal(prior.status, 'completed');
  const solverOperationsBefore = app.store.operations(details.caseId).filter(operation => operation.stageId === 'execute').length;
  await app.kernel.command(details.caseId, 'invalidate', { changedStages: ['wf-05'], reason: 'Verify redraw invalidation and recovery' });
  const stale = await currentGrindingResult(app, details.caseId);
  assert.equal(stale, null);
  const plan = app.store.get('grinding-plans', details.caseId);
  const result = await runGrinding(app, plan, { onProgress: value => console.log(JSON.stringify(value)) });
  assert.equal(result.status, 'awaitingImageInspection');
  const solverOperationsAfter = app.store.operations(details.caseId).filter(operation => operation.stageId === 'execute').length;
  assert.equal(solverOperationsAfter, solverOperationsBefore);
  const record = { caseId: details.caseId, oldCompletedResultRejected: true, solverOperationsBefore, solverOperationsAfter,
    oldStateBinding: prior.stateBinding, newStateBinding: result.stateBinding, figures: result.figures,
    state: app.store.getCase(details.caseId), verifiedAt: new Date().toISOString() };
  await writeFile(join(directory, 'redraw-recovery.json'), JSON.stringify(record, null, 2));
  console.log(JSON.stringify({ caseId: details.caseId, status: result.status, figures: result.figures.map(figure => ({ name: figure.name, path: figure.outputRefs[0].locator })) }));
} finally { app.close(); }
