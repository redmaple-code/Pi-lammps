import { digest, requireCondition } from '../contracts/index.js';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { hashFile } from '../artifacts/index.js';

export async function verifyPlan(app, plan) {
  if (!plan) return;
  requireCondition(digest(JSON.parse(await readFile(plan.parametersFile, 'utf8'))) === digest(plan.scheme.parameters), 'ARTIFACT_CHANGED', 'Frozen parameter file changed');
  for (const ref of plan.toolRefs || []) {
    await app.artifacts.verify(ref);
    requireCondition(await hashFile(join(plan.toolsRoot, ref.metadata.name)) === ref.digest, 'ARTIFACT_CHANGED', 'Frozen analysis/render tool changed');
  }
  if (plan.frozenProtocolRef) {
    const frozen = JSON.parse((await app.artifacts.read(plan.frozenProtocolRef)).toString('utf8'));
    requireCondition(digest(frozen.parameters) === digest(plan.scheme.parameters) && digest(frozen.protocol) === digest(plan.scheme.validationProtocol),
      'ARTIFACT_CHANGED', 'Frozen design and protocol differ from the current plan');
  }
}

export function resultBinding(state) {
  return digest({ revisionId: state.revisionId, lock: state.lock.digest, status: state.status,
    stages: state.stages.map(stage => ({ id: stage.id, status: stage.status, attempt: stage.attempt,
      inputs: stage.inputRefs, outputs: stage.outputRefs, receipts: stage.receipts,
      producer: stage.producerInstanceId || null })) });
}

export async function currentGrindingResult(app, caseId) {
  const state = app.store.getCase(caseId), result = app.store.get('grinding-results', caseId);
  const plan = app.store.get('grinding-plans', caseId);
  await verifyPlan(app, plan);
  if (!result || result.revisionId !== state.revisionId || result.stateBinding !== resultBinding(state)) return null;
  if (plan && result.schemeDigest !== digest(plan.scheme)) return null;
  const completed = state.status === 'completed' && result.status === 'completed';
  const awaitingInspection = state.status === 'active' && result.status === 'awaitingImageInspection'
    && state.stages.every(stage => stage.id === 'wf-05' ? stage.status === 'awaitingInspection' : ['passed', 'skipped'].includes(stage.status));
  if (!completed && !awaitingInspection) return null;
  for (const stage of state.stages) {
    for (const ref of [...stage.inputRefs, ...stage.outputRefs]) await app.artifacts.verify(ref);
    if (stage.reviewRequired && stage.status === 'passed') {
      const review = app.store.get('reviews', stage.receipts.at(-1));
      requireCondition(review?.verdict === 'PASS', 'REVIEW_REQUIRED', 'Completed stage has no passing review');
      await app.gates.validate(review.id, state, stage);
    }
  }
  for (const figure of result.figures || []) for (const ref of figure.outputRefs) await app.artifacts.verify(ref);
  if (result.analysisRef) await app.artifacts.verify(result.analysisRef);
  if (result.stateBinding !== resultBinding(app.store.getCase(caseId))) return null;
  const latestPlan = app.store.get('grinding-plans', caseId);
  if (latestPlan && result.schemeDigest !== digest(latestPlan.scheme)) return null;
  return result;
}

export function saveGrindingResult(app, result) {
  const state = app.store.getCase(result.caseId);
  const plan = app.store.get('grinding-plans', result.caseId);
  const bound = { ...result, revisionId: state.revisionId, stateBinding: resultBinding(state),
    schemeDigest: plan ? digest(plan.scheme) : null };
  return app.store.put('grinding-results', result.caseId, bound, app.store.get('grinding-results', result.caseId)?.recordVersion || 0);
}
