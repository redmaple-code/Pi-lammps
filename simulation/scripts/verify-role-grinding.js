import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import { openApplication } from '../packages/application/index.js';
import { createGrindingPlan, runGrinding } from '../packages/application/grinding.js';
import { openGrindingRuntime } from '../packages/application/grinding-workflow.js';
import { simulationRoot } from './build-packs.js';

const evidenceDirectory = join(simulationRoot, 'audits/2026-11-18-role-workflow');
await mkdir(evidenceDirectory, { recursive: true });
const configPath = join(simulationRoot, 'runtime.example.json');
const config = JSON.parse(await readFile(configPath, 'utf8'));
const parameters = { ...JSON.parse(await readFile(join(simulationRoot, 'domains/lammps/grinding.parameters.json'), 'utf8')),
  cells: [20, 10, 8], equilibrationSteps: 400, approachSteps: 400, grindingSteps: 800, dumpEvery: 100, forceEvery: 20 };
const parametersFile = join(evidenceDirectory, 'requested-parameters.json');
await writeFile(parametersFile, JSON.stringify(parameters, null, 2));
const stateDirectory = join(process.env.LOCALAPPDATA, 'RedMaple/simulation/role-workflow-20261118');
let app = await openApplication({ workspace: simulationRoot, stateDirectory });
const progress = value => console.log(JSON.stringify(value));
function traceTransport(runtime) {
  const fetch = runtime.options.fetch;
  if (!fetch) return;
  runtime.options.fetch = async (input, init) => {
    const request = typeof init?.body === 'string' ? JSON.parse(init.body) : {};
    console.log(JSON.stringify({ event: 'transportRequest', transport: runtime.options.transportProfile,
      bodyBytes: typeof init?.body === 'string' ? Buffer.byteLength(init.body) : null,
      model: request.model, thinking: request.thinking, reasoningEffort: request.reasoning_effort,
      maxTokens: request.max_tokens, toolCount: request.tools?.length }));
    try {
      const response = await fetch(input, init);
      console.log(JSON.stringify({ event: 'transportResponse', status: response.status }));
      return response;
    } catch (error) {
      console.log(JSON.stringify({ event: 'transportFailure', code: error.code || error.cause?.code,
        message: String(error.message).replaceAll(process.env.AISPOT_DS_API_KEY || '\u0000', '[REDACTED]') }));
      throw error;
    }
  };
}
try {
  let plan;
  const existing = app.store.list('grinding-plans').at(-1);
  if (existing) plan = existing;
  else plan = await createGrindingPlan(app, { python: config.simulation.python, lammps: config.simulation.lammps, parametersFile, validation: true });
  await writeFile(join(evidenceDirectory, 'case.json'), JSON.stringify({ caseId: plan.caseId, revisionId: plan.revisionId, stateDirectory }, null, 2));
  console.log(JSON.stringify({ event: 'case', caseId: plan.caseId, stateDirectory }));
  const budgetKey = `${plan.caseId}:${plan.revisionId}`;
  const budget = app.store.get('model-budgets', budgetKey);
  const developmentLimit = 224;
  if (budget && budget.limit !== developmentLimit) app.store.put('model-budgets', budgetKey, { ...budget, limit: developmentLimit,
    adjustments: [...(budget.adjustments || []), { from: budget.limit, to: developmentLimit,
      reason: 'Development integration retries and final independent analysis/redraw verification; original usage retained', at: new Date().toISOString() }] }, budget.recordVersion);
  let runtime = await openGrindingRuntime(app, configPath);
  traceTransport(runtime);
  runtime.options.taskModelCalls = developmentLimit;
  const first = await runGrinding(app, plan, { runtime, stopAfterStage: 'execute', onProgress: progress });
  const solverOperations = app.store.operations(plan.caseId).filter(operation => operation.stageId === 'execute');
  assert.equal(solverOperations.length, 1, 'One solver case was requested');
  assert.equal(solverOperations[0].status, 'succeeded');
  const before = app.store.getCase(plan.caseId);
  const recovery = { caseId: plan.caseId, solverOperationId: solverOperations[0].operationId,
    beforeReopen: before.stages.map(({ id, status, attempt }) => ({ id, status, attempt })), firstResult: first.status };
  app.close();
  app = await openApplication({ workspace: simulationRoot, stateDirectory });
  plan = app.store.get('grinding-plans', plan.caseId);
  runtime = await openGrindingRuntime(app, configPath);
  traceTransport(runtime);
  runtime.options.taskModelCalls = developmentLimit;
  const resumed = await runGrinding(app, plan, { runtime, onProgress: progress });
  assert.equal(app.store.operations(plan.caseId).filter(operation => operation.stageId === 'execute').length, 1, 'Resume duplicated the solver');
  assert.equal(app.store.getCase(plan.caseId).stages.find(stage => stage.id === 'execute').attempt, 1);
  assert.equal(resumed.status, 'awaitingImageInspection');
  const cached = await runGrinding(app, plan, { configFile: configPath });
  assert.equal(cached.stateBinding, resumed.stateBinding);
  const roles = app.store.list('role-operations').filter(operation => operation.caseId === plan.caseId);
  recovery.afterReopen = app.store.getCase(plan.caseId).stages.map(({ id, status, attempt }) => ({ id, status, attempt }));
  recovery.solverDuplicated = false;
  recovery.awaitingImageInspection = true;
  recovery.roleOperations = roles.map(({ id, actor, status, provider, model, stageId }) => ({ id, actor, status, provider, model, stageId }));
  await writeFile(join(evidenceDirectory, 'recovery.json'), JSON.stringify(recovery, null, 2));
  await writeFile(join(evidenceDirectory, 'result.json'), JSON.stringify(resumed, null, 2));
  console.log(JSON.stringify({ event: 'verified', caseId: plan.caseId, status: resumed.status, figures: resumed.figures.map(figure => ({ name: figure.name, path: figure.outputRefs[0].locator })) }));
} catch (error) {
  console.error(JSON.stringify(error.toJSON?.() || { code: error.code, message: error.message }));
  process.exitCode = 1;
} finally { app.close(); }
