import { readFile, writeFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { id, requireCondition, utc } from '../contracts/index.js';
import { checkLammpsInput, inspectStructureData, potentialMetadata } from '../../domains/lammps/checks.js';
import { evaluateMetric, acceptanceSummary, logSections } from '../analysis/index.js';
import { scientificValidation } from '../analysis/scientific-validation.js';
import { hashFile } from '../artifacts/index.js';
import { programOperation } from './grinding.js';

export const grindingOutputFiles = ['log.lammps', 'grinding.csv', 'approach.csv', 'trajectory.lammpstrj',
  'initial.lammpstrj', 'final.lammpstrj', 'final.data', 'final.restart'];

export const hardCheck = (stageId, passed, observations) => ({ checkId: `HOST-${stageId}`, applicability: true,
  result: passed ? 'passed' : 'failed', action: 'blocked', observations });

export function stageOutputs(app, caseId, stageId) {
  return app.store.getCase(caseId).stages.find(stage => stage.id === stageId)?.outputRefs || [];
}

export function stageArtifact(app, caseId, stageId, type) {
  const ref = stageOutputs(app, caseId, stageId).find(item => item.type === type);
  requireCondition(ref, 'REFERENCE_MISSING', `Missing ${type} from ${stageId}`);
  return ref;
}

export async function readStageJson(app, caseId, stageId, type) {
  return JSON.parse((await app.artifacts.read(stageArtifact(app, caseId, stageId, type))).toString('utf8'));
}

export function geometryAssessment(parameters) {
  const p = parameters;
  return { periodicWidthExceedsDiameter: p.cells[1] * p.latticeAngstrom > 2 * p.radiusAngstrom,
    thermalLayerBelowContact: 3 * p.latticeAngstrom < p.cells[2] * p.latticeAngstrom - p.depthAngstrom,
    abrasiveStaysWithinLength: 5 * p.latticeAngstrom + p.speedAngstromPerPs * p.grindingSteps * p.timeStepPs
      + p.radiusAngstrom < p.cells[0] * p.latticeAngstrom,
    positiveTimestep: p.timeStepPs > 0 && p.timeStepPs <= .001 };
}

export async function baselineInput(app, plan, root) {
  const cached = app.store.get('baseline-inputs', plan.revisionId);
  if (cached) { await app.artifacts.verify(cached.ref); return cached.ref; }
  const original = stageOutputs(app, plan.caseId, 'wf-01').find(ref => ref.type === 'baseline-input');
  if (original) return original;
  const directory = join(plan.directory, 'baseline-recovery');
  await programOperation(app, plan.caseId, 'baseline-recovery', [plan.python, join(plan.toolsRoot || join(root, 'science'), 'grinding.py'),
    'prepare', '--directory', directory, '--potential', join(root, 'domains/lammps/assets/FeNiCrCoCu-heafixed.setfl'), '--parameters', plan.parametersFile], root);
  requireCondition(await hashFile(join(directory, 'alloy.data')) === stageArtifact(app, plan.caseId, 'wf-01', 'structure').digest,
    'ARTIFACT_CHANGED', 'Recovered baseline structure differs from the approved structure');
  const ref = await app.artifacts.publish(join(directory, 'in.grinding'), { type: 'baseline-input', caseId: plan.caseId });
  app.store.put('baseline-inputs', plan.revisionId, { ref }, 0);
  return ref;
}

export async function runStageTool(app, plan, stageId, root, { signal, onProgress, inputText } = {}) {
  const caseId = plan.caseId;
  const publish = (value, type) => app.artifacts.json(value, { type, caseId });
  const inputs = join(plan.directory, 'inputs');
  const tool = name => join(plan.toolsRoot || join(root, 'science'), name);
  const check = (passed, observations) => [hardCheck(stageId, passed, observations)];
  if (stageId === 'wf-00') {
    return { refs: [await publish(plan.scheme, 'design')], checks: check(true, ['D1-D7 and exploratory scope are explicit']) };
  }
  if (stageId === 'wf-r') {
    const geometry = geometryAssessment(plan.scheme.parameters);
    requireCondition(Object.values(geometry).every(Boolean), 'SCIENTIFIC_CHECK_FAILED', 'Frozen design fails geometry/time checks', { context: { geometry } });
    return { refs: [await publish({ geometry, limitations: plan.scheme.limitations }, 'assessment')],
      checks: check(true, ['Geometry, periodic width, thermal-layer placement and time-step cap checked']) };
  }
  if (stageId === 'wf-01') {
    await programOperation(app, caseId, stageId, [plan.python, tool('grinding.py'), 'prepare', '--directory', inputs,
      '--potential', join(root, 'domains/lammps/assets/FeNiCrCoCu-heafixed.setfl'), '--parameters', plan.parametersFile], root);
    const structure = JSON.parse(await readFile(join(inputs, 'structure-report.json'), 'utf8'));
    const independent = inspectStructureData(await readFile(join(inputs, 'alloy.data'), 'utf8'), {
      latticeAngstrom: plan.scheme.parameters.latticeAngstrom, inputText: await readFile(join(inputs, 'in.grinding'), 'utf8') });
    return { refs: [await app.artifacts.publish(join(inputs, 'alloy.data'), { type: 'structure', caseId }),
      await publish(structure, 'structure-report'), await publish(independent, 'structure-check'),
      await app.artifacts.publish(join(inputs, 'in.grinding'), { type: 'baseline-input', caseId })],
      checks: check(independent.passed && structure.fixedAtomCount > 0 && structure.mobileAtomCount > 0,
        ['Actual coordinates, composition counts, bounds and atom groups checked']) };
  }
  if (stageId === 'wf-02') {
    const potential = join(inputs, 'FeNiCrCoCu-heafixed.setfl');
    const metadata = await potentialMetadata(potential);
    return { refs: [await app.artifacts.publish(potential, { type: 'potential', caseId }), await publish(metadata, 'potential-mapping')],
      checks: check(metadata.elements.join(' ') === plan.scheme.D1.elements.join(' '), ['Potential header and ordered element mapping checked']) };
  }
  if (stageId === 'wf-03a') {
    const structure = await readStageJson(app, caseId, 'wf-01', 'structure-report');
    const independent = await readStageJson(app, caseId, 'wf-01', 'structure-check');
    const metadata = await readStageJson(app, caseId, 'wf-02', 'potential-mapping');
    const text = inputText ?? (await app.artifacts.read(await baselineInput(app, plan, root))).toString('utf8');
    const checks = checkLammpsInput(text, { mapping: metadata.elements, potentialElements: metadata.elements,
      masses: structure.massMapping, features: plan.scheme.features, structureReport: independent,
      requiredOutputs: ['grinding.csv', 'trajectory.lammpstrj', 'c_pe', 'c_coord', 'c_centro', 'c_cna'] });
    return { refs: [await app.artifacts.text(text, { type: 'solver-input', caseId }),
      await publish({ checks, protocol: plan.scheme.validationProtocol }, 'input-checks')],
      checks: [...check(checks.every(item => ['passed', 'notApplicable'].includes(item.result)), ['LAMMPS required command checks and output closure checked']), ...checks] };
  }
  if (stageId === 'execute') return executeStage(app, plan, { signal, onProgress });
  const run = await readStageJson(app, caseId, 'execute', 'run');
  const workdir = join(app.executor.directory(run.operationId), 'work');
  if (stageId === 'wf-04') {
    for (const comparison of plan.scheme.validationProtocol.comparisons) {
      for (const ref of comparison.referenceRefs || []) await app.artifacts.verify(ref);
    }
    await programOperation(app, caseId, stageId, [plan.python, tool('grinding.py'), 'analyze', '--directory', workdir], root);
    const measured = JSON.parse(await readFile(join(workdir, 'analysis.json'), 'utf8'));
    const criteria = plan.scheme.criteria.map(definition => evaluateMetric(definition, measured[definition.field], run.outputRefs, run.id));
    const log = await readFile(join(workdir, 'log.lammps'), 'utf8');
    const diagnostics = { errors: log.split(/\r?\n/).filter(line => /^\s*ERROR(?:\s|:)/.test(line)),
      warnings: log.split(/\r?\n/).filter(line => /^\s*WARNING(?:\s|:)/.test(line)), complete: log.includes('REDMAPLE_GRINDING_COMPLETE') };
    const samples = (await readFile(join(workdir, 'grinding.csv'), 'utf8')).trim().split(/\r?\n/).slice(1)
      .filter(line => line.trim()).map(line => line.trim().split(/\s+/).map(Number));
    requireCondition(samples.length >= 2 && samples.every(row => row.length === 8 && row.every(Number.isFinite)), 'OUTPUT_MISSING', 'Motion audit needs finite force samples');
    const first = samples[0], last = samples.at(-1), p = plan.scheme.parameters;
    const forceTail = samples.slice(Math.floor(samples.length / 2));
    const executedInput = stageArtifact(app, caseId, 'wf-03a', 'solver-input');
    const inputText = (await app.artifacts.read(executedInput)).toString('utf8');
    const measurementSemantics = {
      executedInputRef: executedInput,
      commandDefinitions: inputText.split(/\r?\n/).filter(line => /^(units|group|fix|compute|variable|thermo_style)\s/.test(line)),
      force: { source: 'fix tool global vector: force ON the indenter, already a reaction load',
        normal: 'mean(f_tool[3]) over forceMeanWindow; signed +z upward reaction',
        tangential: 'mean(-f_tool[1]) over forceMeanWindow; signed resistance to +x motion, not sqrt(Fx^2+Fy^2)',
        peakNormal: 'max(f_tool[3]) over fullPassWindow', conversionNnPerEVPerAngstrom: 1.602176634,
        scope: 'Idealized rigid repulsive sphere reaction loads; not hardness, contact stress, or experimentally validated grinding force' },
      fullPassWindow: { globalStepStart: first[0], globalStepEnd: last[0], sampleCount: samples.length },
      forceMeanWindow: { globalStepStart: forceTail[0][0], globalStepEnd: forceTail.at(-1)[0],
        passRelativeStepStart: forceTail[0][0] - first[0], passRelativeStepEnd: last[0] - first[0], sampleCount: forceTail.length },
      temperature: { source: 'compute Tmobile mobile temp', selection: 'all mobile atoms, including bath and dynamic groups',
        averaging: 'fullPassWindow', targetK: p.temperatureK, firstK: first[7], lastK: last[7],
        meanOffsetPercent: (measured.meanTemperatureK / p.temperatureK - 1) * 100,
        scope: 'Driven short trajectory; equilibration or steady state is not demonstrated. Actual thermostat groups/damping are listed in commandDefinitions.' },
      energy: { unit: 'eV for units metal', scope: 'Default fix indent energy is excluded unless fix_modify tool energy yes is present; no total-energy conservation claim' },
      interpretationLimits: ['Nominal depth is measured against the initial surface; contact-atom penetration and pile-up are not inferred from force magnitude alone.']
    };
    const motion = { timeStepPs: (last[1] - first[1]) / (last[0] - first[0]),
      speedAngstromPerPs: (last[2] - first[2]) / (last[1] - first[1]), travelAngstrom: last[2] - first[2] };
    motion.speedMPerS = motion.speedAngstromPerPs * 100;
    motion.passed = Math.abs(motion.timeStepPs - p.timeStepPs) <= 1e-9 && Math.abs(motion.speedAngstromPerPs - p.speedAngstromPerPs) <= 1e-6
      && Math.abs(motion.travelAngstrom - measured.tipTravelAngstrom) <= 1e-6;
    const validation = scientificValidation(plan.scheme.validationProtocol, measured, { dataRefs: run.outputRefs, runRef: run.id, parameters: plan.scheme.parameters,
      measurementUnits: { meanNormalForceNn: 'nN', meanTangentialForceNn: 'nN', peakNormalForceNn: 'nN', meanTemperatureK: 'K',
        maxDisplacementAngstrom: 'angstrom', fixedMaxDisplacementAngstrom: 'angstrom', tipTravelAngstrom: 'angstrom' } });
    const integration = acceptanceSummary(criteria, validation);
    const analysis = { ...measured, runRef: run.id, criteria, acceptance: { ...integration, integration: integration.task,
      task: validation.task === 'passed' ? 'met' : validation.task === 'failed' ? 'notMet' : 'cannotCompute' },
      validation, motionAudit: motion, diagnostics, measurementSemantics,
      logReadScope: { ...logSections(log), diagnosticScan: 'allLines',
        fullLogRef: run.outputRefs.find(ref => ref.metadata?.name === 'log.lammps') }, dataRefs: run.outputRefs };
    app.store.put('analyses', caseId, analysis);
    return { refs: [await publish(analysis, 'analysis')], checks: check(diagnostics.complete && !diagnostics.errors.length && measured.finite && motion.passed,
      ['Full log scanned for ERROR/WARNING, completion marker and finite measurements checked', 'Motion/time-step/speed independently reconstructed from actual samples']) };
  }
  requireCondition(stageId === 'wf-05', 'CAPABILITY_UNAVAILABLE', `Unsupported stage ${stageId}`);
  await programOperation(app, caseId, stageId, [plan.python, tool('ovito_render.py'), '--directory', workdir], root);
  const analysisRef = stageArtifact(app, caseId, 'wf-04', 'analysis');
  const figures = [];
  for (const name of (await readdir(join(workdir, 'figures'))).filter(name => name.endsWith('.png'))) {
    const ref = await app.artifacts.publish(join(workdir, 'figures', name), { type: 'image', caseId, name, runRef: run.id, analysisRef: analysisRef.artifactId });
    figures.push({ id: id('figure-draft'), name, outputRefs: [ref], runRef: run.id, analysisRef: analysisRef.artifactId,
      inspection: { opened: false } });
  }
  requireCondition(figures.length > 0, 'OUTPUT_MISSING', 'Renderer produced no figures');
  app.store.put('figure-drafts', caseId, { id: caseId, figures });
  return { refs: [await publish(figures, 'figure-manifest')], checks: check(true, ['Actual image artifacts generated; visual inspection remains pending']) };
}

async function executeStage(app, plan, { signal, onProgress }) {
  const caseId = plan.caseId;
  const state = app.store.getCase(caseId), stage = state.stages.find(item => item.id === 'execute');
  let operationId = stage.operationId;
  if (!operationId) {
    const inputRef = stageArtifact(app, caseId, 'wf-03a', 'solver-input');
    await writeFile(join(plan.directory, 'inputs/in.grinding'), await app.artifacts.read(inputRef));
    const structureRef = stageArtifact(app, caseId, 'wf-01', 'structure-report');
    const closure = await app.artifacts.closure([
      { path: 'in.grinding', ref: inputRef },
      { path: 'alloy.data', ref: stageArtifact(app, caseId, 'wf-01', 'structure') },
      { path: 'structure-report.json', ref: structureRef },
      { path: 'FeNiCrCoCu-heafixed.setfl', ref: stageArtifact(app, caseId, 'wf-02', 'potential') }]);
    const resources = { wallSeconds: 1800, cpus: 1 };
    const permit = app.kernel.permit(caseId, { closure, authorizationRef: plan.scheme.authorizationRef, resources });
    operationId = id('lammps');
    const request = { argv: [plan.lammps, '-in', 'in.grinding', '-log', 'log.lammps'], timeoutMs: resources.wallSeconds * 1000,
      permitRef: permit.id, inputClosure: closure, environment: { OMP_NUM_THREADS: '1' } };
    await app.kernel.command(caseId, 'attachOperation', { stageId: stage.id, operationId, request });
  }
  const operation = app.store.operation(operationId);
  await app.executor.submit(operation.request, operationId);
  onProgress?.({ caseId, stage: 'execute', operationId, status: 'submitted' });
  const receipt = await app.executor.wait(operationId, { signal, onUpdate: onProgress });
  requireCondition(receipt.status === 'succeeded', receipt.status === 'unknown' ? 'RESULT_UNKNOWN' : 'TOOL_FAILED',
    `LAMMPS ${receipt.status}`, { sideEffectState: receipt.status === 'unknown' ? 'unknown' : 'confirmed' });
  const run = app.store.list('runs').find(item => item.operationId === operationId)
    || await app.executor.collect(operationId, grindingOutputFiles.map(path => ({ path })));
  if (!app.store.get('runs', run.id)) app.store.put('runs', run.id, run, 0);
  return { refs: [await app.artifacts.json(run, { type: 'run', caseId })],
    checks: [hardCheck('execute', true, [`Confirmed solver receipt ${operationId}`, `Exit code ${receipt.exitCode}`])] };
}
