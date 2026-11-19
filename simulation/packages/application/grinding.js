import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile, mkdir, readdir, copyFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { id, digest, utc, requireCondition } from '../contracts/index.js';
import { recordAuthorization } from '../kernel/authorization.js';
import { resolvePack } from '../pack-resolver/index.js';
import { writeReport } from '../presentation/index.js';
import { hashFile } from '../artifacts/index.js';
import { validateSchema } from '../contracts/validation.js';

const execute = promisify(execFile);
const root = fileURLToPath(new URL('../..', import.meta.url));
const outputFiles = ['log.lammps', 'grinding.csv', 'approach.csv', 'trajectory.lammpstrj', 'initial.lammpstrj', 'final.lammpstrj', 'final.data', 'final.restart'];
const requiredChecks = Array.from({ length: 9 }, (_, i) => `MB-${String(i + 1).padStart(3, '0')}`);
export async function createGrindingPlan(app, { python, lammps, parametersFile = join(root, 'domains/lammps/grinding.parameters.json'), validation = true, parentCaseId } = {}) {
  requireCondition(python && lammps, 'CAPABILITY_UNAVAILABLE', 'Configure Python and LAMMPS executable paths');
  const parameters = JSON.parse(await readFile(parametersFile, 'utf8'));
  validateGrindingParameters(parameters);
  const resolved = await resolvePack(join(root, 'domains/lammps/manifest.json'), { validation,
    capabilityRegistry: { 'local-lammps': { version: '0.1.0', capabilities: ['molecular.dynamics.solve'] } } });
  requireCondition(resolved.executionReady, 'REFERENCE_MISSING', 'Package is not ready', { context: { diagnostics: resolved.diagnostics } });
  const caseId = parentCaseId || id('hea-grinding');
  const directory = parentCaseId ? join(app.workspace, 'work', 'cases', caseId, 'versions', id('version')) : join(app.workspace, 'work', 'cases', caseId);
  await mkdir(join(directory, 'inputs'), { recursive: true });
  const toolsRoot = join(directory, 'tools');
  await mkdir(toolsRoot, { recursive: true });
  const toolRefs = [];
  for (const [name, source] of [['grinding.py', join(root, 'science/grinding.py')], ['ovito_render.py', join(root, 'science/ovito_render.py')],
    ['ovito_adapter.py', join(root, 'packages/presentation/ovito_adapter.py')]]) {
    const target = join(toolsRoot, name);
    await copyFile(source, target, 1);
    toolRefs.push(await app.artifacts.publish(target, { type: 'analysis-tool-source', caseId, name }));
  }
  const features = ['five-components', 'fcc-substrate', 'fixed-boundary', 'thermal-layer', 'spherical-abrasive', 'lateral-motion']
    .map(featureId => ({ id: featureId, criticality: 'critical', verificationMethod: 'data-and-input-check', requiredStage: ['wf-01', 'wf-03a'] }));
  const criteria = grindingCriteria(parameters);
  const human = { actorId: 'workspace-user', actorInstanceId: id('user-request'), kind: 'human' };
  const authorization = recordAuthorization(app.store, { source: 'User requested complete development and a HEA grinding test, delegated all composition/tool/potential/conditions to pi-lammps.',
    actions: ['solve', 'prepare', 'analyze', 'render'], caseId, resources: { wallSeconds: 1800, cpus: 1 } }, human);
  const scheme = { goal: 'Exercise the full scientific workflow with a real single-asperity grinding pass',
    scope: 'candidateValidation', parameters, features, criteria, authorizationRef: authorization.id,
    D1: { material: 'equiatomic FCC FeNiCrCoCu model alloy', elements: ['Fe', 'Ni', 'Cr', 'Co', 'Cu'] },
    D2: 'Spherical abrasive approaches the free surface and traverses in +x', D21: features,
    D3: { temperatureK: parameters.temperatureK }, D4: { cells: parameters.cells, boundary: 'free x/z, periodic y' },
    D5: { timestepPs: parameters.timeStepPs, thermostat: 'boundary Langevin, NVE mobile', approach: parameters.approachSteps, grinding: parameters.grindingSteps },
    D6: { potential: 'Farkas-Caro 2018 corrected EAM/alloy', source: 'https://www.ctcms.nist.gov/potentials/Download/2018--Farkas-D-Caro-A--Fe-Ni-Cr-Co-Cu/2/FeNiCrCoCu-heafixed.setfl', doi: '10.1557/jmr.2018.245' },
    D7: criteria, limitations: ['Idealized model-alloy potential', 'Rigid repulsive sphere represents the abrasive geometry',
      'Single short small-cell integration test. Convergence, repeat-seed statistics and experimental grinding comparison are pending.'] };
  const { stageEvidencePolicy, exploratoryProtocol } = await import('./grinding-workflow.js');
  scheme.validationProtocol = exploratoryProtocol();
  scheme.D7 = { integration: criteria, scientific: scheme.validationProtocol };
  const workflow = structuredClone(resolved.workflow);
  for (const stage of workflow.stages) {
    stage.requiredForExecution = ['wf-00', 'wf-r', 'wf-01', 'wf-02', 'wf-03a'].includes(stage.id);
    if (stage.id === 'wf-04') stage.reviewRequired = true;
    if (stage.reviewRequired) stage.evidencePolicy = stageEvidencePolicy(stage.id);
    stage.requiredCheckIds = [stage.id, `HOST-${stage.id}`, ...(stage.id === 'wf-03a' ? [...requiredChecks, 'OUTPUT-CLOSURE'] : [])];
    stage.requiredApplicableCheckIds = [stage.id];
  }
  const requestRef = await app.artifacts.json({ intent: 'Single-case HEA grinding exploratory validation', parameters,
    delegation: 'User delegated alloy/tool/potential/conditions to Pi-lammps', validationScope: 'one solver case and recovery tests' }, { type: 'task-request', caseId });
  const externalInputs = { request: [requestRef] };
  const state = parentCaseId ? app.store.forkRevision(caseId, { workflow, lock: resolved.lock, authorization: authorization.id, externalInputs,
    reason: 'User requested modified grinding conditions' })
    : app.store.createCase({ caseId, title: 'FeNiCrCoCu 高熵合金单磨粒磨削验证', lock: resolved.lock, workflow, authorization: authorization.id, externalInputs });
  const frozenParameters = join(directory, 'parameters.json');
  await writeFile(frozenParameters, JSON.stringify(parameters, null, 2), { flag: 'wx' });
  const plan = { caseId, directory, python: resolve(python), lammps: resolve(lammps), parametersFile: frozenParameters,
    toolsRoot, toolRefs, scheme, stateRef: state.id, revisionId: state.revisionId,
    workflowVersion: 'roles/2', status: 'planned', createdAt: utc() };
  app.store.put('grinding-plans', caseId, plan, app.store.get('grinding-plans', caseId)?.recordVersion || 0);
  app.store.put('revision-plans', state.revisionId, plan, 0);
  await writeFile(join(directory, 'PLAN.json'), JSON.stringify(plan, null, 2));
  return plan;
}

export async function programOperation(app, caseId, stageId, argv, cwd) {
  const attempt = app.store.getCase(caseId).stages.find(stage => stage.id === stageId)?.attempt || null;
  const request = { argv, cwd, attempt, implementationDigest: await hashFile(argv[1]) };
  const existing = app.store.operations(caseId).find(operation => operation.stageId === stageId && digest(operation.request) === digest(request));
  if (existing) {
    requireCondition(existing.status === 'succeeded', 'RESULT_UNKNOWN', `Reconcile original ${stageId} program operation before repeating`, { sideEffectState: 'unknown' });
    return existing.receipt;
  }
  const state = app.store.getCase(caseId), operationId = id('program');
  app.store.dispatch(caseId, { id: id('command'), action: 'programIntent', operationId }, state.revision,
    () => ({ events: [{ kind: 'programIntent', payload: { stageId, operationId, argv } }], intents: [{ operationId, request, stageId }] }));
  app.store.claim(operationId, { pid: process.pid, token: id('program-owner') });
  try {
    const result = await execute(argv[0], argv.slice(1), { cwd, windowsHide: true, timeout: 300000, maxBuffer: 4 * 1024 * 1024,
      env: { ...process.env, QT_QPA_PLATFORM: 'offscreen', MPLBACKEND: 'Agg' } });
    const receipt = { operationId, status: 'succeeded', argv, cwd, stdout: result.stdout, stderr: result.stderr, completedAt: utc() };
    app.store.settle(operationId, receipt);
    return receipt;
  } catch (error) {
    const receipt = { operationId, status: error.killed ? 'unknown' : 'failed', argv, cwd, error: error.message, stderr: error.stderr || '', completedAt: utc() };
    app.store.settle(operationId, receipt);
    throw error;
  }
}
async function completeStage(app, caseId, stageId, outputRefs) {
  const stage = app.store.getCase(caseId).stages.find(item => item.id === stageId);
  if (['passed', 'awaitingReview'].includes(stage.status)) return { state: app.store.getCase(caseId) };
  const completion = { id: id('completion'), caseId, stageId, outputRefs, completedAt: utc() };
  app.store.put('completions', completion.id, completion, 0);
  return app.kernel.command(caseId, 'produced', { stageId, outputRefs, completionReceiptRef: completion.id });
}
export async function runGrinding(app, plan, options = {}) {
  const { runRoleGrinding } = await import('./grinding-workflow.js');
  return runRoleGrinding(app, plan, options);
}

export async function inspectGrindingFigures(app, caseId, { actor, observations }) {
  requireCondition(actor?.actorInstanceId && observations?.length, 'EVIDENCE_INSUFFICIENT', 'Actual image inspection observations required');
  const result = app.store.get('grinding-results', caseId), plan = app.store.get('grinding-plans', caseId);
  const drafts = app.store.get('figure-drafts', caseId);
  requireCondition(result && plan && drafts, 'REFERENCE_MISSING', 'No grinding figures to inspect');
  if (plan.workflowVersion === 'roles/2') {
    const { currentGrindingResult } = await import('./result-state.js');
    requireCondition(await currentGrindingResult(app, caseId), 'ARTIFACT_CHANGED', 'Image inspection result is stale');
    requireCondition(digest(drafts.figures) === digest(result.figures), 'ARTIFACT_CHANGED', 'Image drafts differ from the current result');
  }
  const figures = [];
  for (const figure of drafts.figures) {
    const observation = observations.find(item => item.name === figure.name);
    requireCondition(observation?.opened === true && observation.checked?.length, 'EVIDENCE_INSUFFICIENT', `Image ${figure.name} has not been inspected`);
    await app.artifacts.verify(figure.outputRefs[0]);
    figures.push({ ...figure, status: 'generated', inspection: { ...observation, actor, inspectedAt: utc() } });
  }
  const figureRef = await app.artifacts.json(figures, { type: 'figure-manifest', caseId });
  const stage = app.store.getCase(caseId).stages.find(item => item.id === 'wf-05');
  if (stage.status === 'awaitingInspection') {
    const receipt = { id: id('inspection'), figures, actor, inspectedAt: utc() };
    app.store.put('completions', receipt.id, receipt, 0);
    await app.kernel.command(caseId, 'inspected', { stageId: 'wf-05', outputRefs: [figureRef], inspectionRef: receipt.id });
  } else await completeStage(app, caseId, 'wf-05', [figureRef]);
  app.store.put('figures', caseId, { id: caseId, figures });
  const state = app.store.getCase(caseId), run = app.store.get('runs', result.runRef), analysis = app.store.get('analyses', caseId);
  await writeReport(plan.directory, { state, scheme: plan.scheme, run, analysis, figures });
  const { saveGrindingResult } = await import('./result-state.js');
  return saveGrindingResult(app, { ...result, status: state.status === 'completed' ? 'completed' : state.status, figures });
}

export async function refreshGrindingFigures(app, caseId) {
  const plan = app.store.get('grinding-plans', caseId), result = app.store.get('grinding-results', caseId);
  requireCondition(plan && result, 'REFERENCE_MISSING', 'Grinding result is missing');
  const state = app.store.getCase(caseId), stage = state.stages.find(item => item.id === 'wf-05');
  requireCondition(stage.status === 'running', 'STATE_CONFLICT', 'Presentation stage must be running');
  await programOperation(app, caseId, 'wf-05', [plan.python, join(plan.toolsRoot || join(root, 'science'), 'ovito_render.py'), '--directory', result.workdir], root);
  const figures = [];
  for (const file of (await readdir(join(result.workdir, 'figures'))).filter(name => name.endsWith('.png'))) {
    const ref = await app.artifacts.publish(join(result.workdir, 'figures', file), { type: 'image', name: file,
      caseId, runRef: result.runRef, analysisRef: result.analysisRef.artifactId,
      tool: file.includes('ovito') ? 'OVITO' : 'matplotlib' });
    figures.push({ id: id('figure-draft'), name: file, outputRefs: [ref], runRef: result.runRef,
      analysisRef: result.analysisRef.artifactId, inspection: { opened: false } });
  }
  app.store.put('figure-drafts', caseId, { id: caseId, figures });
  return figures;
}

export function validateGrindingParameters(parameters) {
  const positive = { type: 'number', exclusiveMinimum: 0 };
  const integer = { type: 'integer', minimum: 1 };
  validateSchema({ type: 'object', additionalProperties: false, required: ['alloy', 'composition', 'latticeAngstrom', 'cells', 'temperatureK', 'seed',
    'timeStepPs', 'equilibrationSteps', 'approachSteps', 'grindingSteps', 'radiusAngstrom', 'depthAngstrom', 'speedAngstromPerPs', 'stiffnessEVPerAngstromCubed', 'dumpEvery', 'forceEvery'],
    properties: { alloy: { const: 'FeNiCrCoCu' }, composition: { const: [.2, .2, .2, .2, .2] }, latticeAngstrom: positive,
      cells: { type: 'array', minItems: 3, maxItems: 3, items: { type: 'integer', minimum: 4, maximum: 200 } }, temperatureK: positive,
      seed: { type: 'integer', minimum: 1, maximum: 899999900 }, timeStepPs: { type: 'number', exclusiveMinimum: 0, maximum: .001 },
      equilibrationSteps: integer, approachSteps: integer, grindingSteps: integer, radiusAngstrom: positive, depthAngstrom: positive,
      speedAngstromPerPs: positive, stiffnessEVPerAngstromCubed: positive, dumpEvery: integer, forceEvery: integer } }, parameters);
  requireCondition(parameters.cells.reduce((a, b) => a * b, 4) <= 1000000, 'RESOURCE_LIMIT', 'This local profile supports at most one million atoms');
  requireCondition(parameters.cells.reduce((a, b) => a * b, 4) % 5 === 0, 'SCHEMA_INVALID', 'Equiatomic five-component construction requires an atom count divisible by five');
  return parameters;
}

export function grindingCriteria(parameters) {
  const criteria = [
    { id: 'finite', field: 'finite', quantity: 'finite outputs', unit: '1', acceptanceSpec: { equals: true } },
    { id: 'atoms', field: 'atomCount', quantity: 'retained atoms', unit: '1', acceptanceSpec: { equals: parameters.cells.reduce((a, b) => a * b, 4) } },
    { id: 'fixed', field: 'fixedMaxDisplacementAngstrom', quantity: 'fixed layer drift', unit: 'angstrom', acceptanceSpec: { maximum: 1e-6 } },
    { id: 'contact', field: 'contactSamples', quantity: 'contact force samples', unit: '1', acceptanceSpec: { minimum: 10 } },
    { id: 'travel', field: 'tipTravelAngstrom', quantity: 'grinding distance', unit: 'angstrom', acceptanceSpec: { minimum: parameters.grindingSteps * parameters.timeStepPs * parameters.speedAngstromPerPs * .95 } }
  ].map(definition => ({ ...definition, methodRef: 'grinding-force-temperature-displacement/1',
    reference: 'explicit integration-validation protocol', selection: 'frozen layer or full grinding pass as defined',
    sampling: { everySteps: parameters.forceEvery }, requiredOutputs: outputFiles }));
  return criteria;
}
