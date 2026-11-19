import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { id, utc, requireCondition } from '../contracts/index.js';
import { diagnoseFailure } from '../kernel/repair.js';
import { evaluateMetric, acceptanceSummary, logSections } from '../analysis/index.js';
import { refreshGrindingFigures, programOperation, runGrinding } from './grinding.js';

export async function analyzeExistingCase(app, caseId, { configFile } = {}) {
  const result = app.store.get('grinding-results', caseId), plan = app.store.get('grinding-plans', caseId);
  requireCondition(result && plan, 'REFERENCE_MISSING', 'Known run and scheme are required for this analysis entry');
  if (plan.workflowVersion === 'roles/2') {
    await app.kernel.command(caseId, 'invalidate', { changedStages: ['wf-04'], reason: 'User requested a new analysis' });
    return runGrinding(app, plan, { configFile, stopAfterStage: 'wf-04' });
  }
  const tool = join(plan.toolsRoot || fileURLToPath(new URL('../../science', import.meta.url)), 'grinding.py');
  const { stdout } = await programOperation(app, caseId, 'partial-analysis', [plan.python, tool, 'analyze', '--directory', result.workdir], app.workspace);
  const measured = JSON.parse(stdout.trim());
  const run = app.store.get('runs', result.runRef);
  const criteria = plan.scheme.criteria.map(definition => evaluateMetric(definition, measured[definition.field], run.outputRefs, run.id));
  const analysis = { ...measured, id: id('analysis'), scope: 'analyze', runRef: run.id, criteria,
    acceptance: acceptanceSummary(criteria, { numerical: 'insufficient', model: 'insufficient' }), analyzedAt: utc() };
  app.store.put('analysis-history', analysis.id, analysis, 0);
  return analysis;
}
export async function visualizeExistingCase(app, caseId, { configFile } = {}) {
  const state = app.store.getCase(caseId);
  const stage = state.stages.find(item => item.id === 'wf-05');
  requireCondition(stage, 'REFERENCE_MISSING', 'Presentation stage is missing');
  const plan = app.store.get('grinding-plans', caseId);
  if (plan?.workflowVersion === 'roles/2') {
    await app.kernel.command(caseId, 'invalidate', { changedStages: ['wf-05'], reason: 'User requested presentation refresh' });
    return runGrinding(app, plan, { configFile });
  }
  if (stage.status === 'passed') await app.kernel.command(caseId, 'invalidate', { changedStages: ['wf-05'], reason: 'User requested presentation refresh' });
  await app.kernel.command(caseId, 'start', { stageId: 'wf-05', producerInstanceId: id('post-processor') });
  const figures = await refreshGrindingFigures(app, caseId);
  const result = app.store.get('grinding-results', caseId);
  app.store.put('grinding-results', caseId, { ...result, status: 'awaitingImageInspection' }, result.recordVersion);
  return { caseId, scope: 'visualize', status: 'awaitingImageInspection', figures };
}
export async function diagnoseExistingCase(app, caseId) {
  const state = app.store.getCase(caseId);
  const operationId = state.stages.find(item => item.id === 'execute')?.operationId;
  requireCondition(operationId, 'REFERENCE_MISSING', 'Case has no submitted execution');
  const path = join(app.executor.directory(operationId), 'work', 'log.lammps');
  const text = await readFile(path, 'utf8');
  const observation = diagnoseFailure({ runRef: operationId, log: text, readScope: logSections(text) });
  app.store.put('diagnoses', observation.id, observation, 0);
  return observation;
}

export async function executeReviewedCase(app, caseId, { onProgress, configFile } = {}) {
  const state = app.store.getCase(caseId), plan = app.store.get('grinding-plans', caseId);
  const priorId = state.stages.find(stage => stage.id === 'execute')?.operationId;
  const prior = priorId && app.store.operation(priorId);
  requireCondition(prior && plan, 'REFERENCE_MISSING', 'An explicitly reviewed execution input is required');
  if (plan.workflowVersion === 'roles/2') {
    await app.kernel.command(caseId, 'invalidate', { changedStages: ['execute'], reason: 'User requested execution of reviewed inputs' });
    await runGrinding(app, plan, { configFile, onProgress, stopAfterStage: 'execute' });
    const { readStageJson } = await import('./grinding-stages.js');
    const run = await readStageJson(app, caseId, 'execute', 'run');
    return { caseId, run, completedScope: 'execute', scientificAcceptance: 'notAssessed' };
  }
  const permit = app.kernel.permit(caseId, { closure: prior.request.inputClosure, authorizationRef: plan.scheme.authorizationRef,
    resources: { wallSeconds: 1800, cpus: 1 } });
  const request = { ...prior.request, permitRef: permit.id }, operationId = id('execution-only');
  app.store.dispatch(caseId, { id: id('command'), action: 'executionOnly', operationId }, state.revision,
    () => ({ events: [{ kind: 'executionOnlyRequested', payload: { operationId } }], intents: [{ operationId, request, scope: 'execute' }] }));
  await app.executor.submit(request, operationId);
  const receipt = await app.executor.wait(operationId, { onUpdate: onProgress });
  requireCondition(receipt.status === 'succeeded', 'TOOL_FAILED', `Execution-only run ${receipt.status}`);
  const outputs = plan.scheme.criteria[0].requiredOutputs.map(path => ({ path }));
  const run = await app.executor.collect(operationId, outputs);
  app.store.put('runs', run.id, { ...run, scope: 'execute', parentRunOperationId: priorId }, 0);
  return { caseId, run, completedScope: 'execute', scientificAcceptance: 'notAssessed' };
}
