import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { fixture } from '../../tests/fixtures.js';
import { runGrinding } from '../../packages/application/grinding.js';

const cleanup = [];
const app = await fixture({ after: action => cleanup.push(action) }, {
  stages: [{ id: 'wf-05', needs: [], reviewRequired: false }]
});
try {
  const caseId = app.state.id;
  await app.kernel.command(caseId, 'start', { stageId: 'wf-05', producerInstanceId: 'audit-producer' });
  const ref = await app.artifacts.text('original presentation');
  app.store.put('completions', 'original', { id: 'original' }, 0);
  await app.kernel.command(caseId, 'produced', { stageId: 'wf-05', outputRefs: [ref], completionReceiptRef: 'original' });
  app.store.put('grinding-results', caseId, { caseId, revisionId: app.state.revisionId, status: 'completed' }, 0);
  await app.kernel.command(caseId, 'invalidate', { changedStages: ['wf-05'], reason: 'Audit simulation of presentation invalidation before successful refresh' });
  const returned = await runGrinding(app, { caseId });
  const current = app.store.getCase(caseId);
  const result = { caseState: current.status, presentationStageState: current.stages[0].status,
    returnedStatus: returned.status, staleCompletionReproduced: current.status === 'active' && returned.status === 'completed',
    scope: 'Isolated temporary fixture. Original scientific cases remain unchanged.' };
  await writeFile(fileURLToPath(new URL('./workflow-probe.json', import.meta.url)), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} finally { for (const close of cleanup) close(); }
