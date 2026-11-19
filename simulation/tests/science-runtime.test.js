import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { fixture } from './fixtures.js';
import { simulationRoot } from '../scripts/build-packs.js';
import { checkLammpsInput, potentialMetadata } from '../domains/lammps/checks.js';
import { expandStudy, runIterations, validateExchange, validateCheckpointBundle } from '../packages/kernel/studies.js';
import { evaluateMetric, outputSupport, acceptanceSummary } from '../packages/analysis/index.js';
import { RedMapleRuntimePort } from '../packages/runtime-redmaple/index.js';
import { assistant, scripted, model, call } from '../../redmaple-agent/test/fixtures.js';

test('actual HEA generator supplies explicit mapping, geometry and all diagnostic commands', async t => {
  const f = await fixture(t);
  const parameters = JSON.parse(await readFile(join(simulationRoot, 'domains/lammps/grinding.parameters.json'), 'utf8'));
  parameters.cells = [12, 10, 8];
  const file = join(f.root, 'parameters.json'); await writeFile(file, JSON.stringify(parameters));
  execFileSync('python', [join(simulationRoot, 'science/grinding.py'), 'prepare', '--directory', f.root,
    '--potential', join(simulationRoot, 'domains/lammps/assets/FeNiCrCoCu-heafixed.setfl'), '--parameters', file], { windowsHide: true });
  const input = await readFile(join(f.root, 'in.grinding'), 'utf8');
  const structure = JSON.parse(await readFile(join(f.root, 'structure-report.json'), 'utf8'));
  const potential = await potentialMetadata(join(f.root, 'FeNiCrCoCu-heafixed.setfl'));
  const context = { mapping: potential.elements, potentialElements: potential.elements, masses: structure.massMapping,
    features: structure.verifiedFeatures.map(id => ({ id, criticality: 'critical' })), structureReport: structure };
  const good = checkLammpsInput(input, context);
  assert.ok(good.every(item => ['passed', 'notApplicable'].includes(item.result)), JSON.stringify(good));
  const badVelocity = checkLammpsInput(input.replace('velocity mobile create', 'velocity all create'), context);
  assert.equal(badVelocity.find(item => item.checkId === 'MB-002').result, 'failed');
  const badMapping = checkLammpsInput(input.replace('Fe Ni Cr Co Cu', 'Ni Fe Cr Co Cu'), context);
  assert.equal(badMapping.find(item => item.checkId === 'MB-006').result, 'failed');
  const missingFeature = checkLammpsInput(input, { ...context, structureReport: { verifiedFeatures: [] } });
  assert.equal(missingFeature.find(item => item.checkId === 'MB-008').result, 'failed');
  assert.equal(checkLammpsInput(input.replace('compute coord all coord/atom cutoff 3.2',''), context).find(item => item.checkId === 'MB-009').result, 'failed');
});
test('measurement criteria preserve missing, borderline and independent validation dimensions', () => {
  const definition = { id: 'd7', quantity: 'force', unit: 'nN', methodRef: 'mean/1', selection: 'z component', reference: 'frozen task criterion', sampling: { everySteps: 10 },
    requiredOutputs: ['fz'], acceptanceSpec: { minimum: 1, maximum: 2, borderlineTolerance: .01 } };
  assert.equal(outputSupport([definition], ['fx'])[0].supported, false);
  assert.equal(evaluateMetric(definition, undefined, [], 'run').verdict, 'cannotCompute');
  assert.equal(evaluateMetric(definition, 2, [], 'run').verdict, 'borderline');
  const result = evaluateMetric(definition, 3, [], 'run');
  assert.equal(acceptanceSummary([result], { numerical: 'passed', model: 'insufficient' }).task, 'notMet');
});
test('bounded studies and coupled checkpoints refuse false convergence and semantic loss', async () => {
  const samples = expandStudy({ studyId: 'study', parameters: { temperature: [300, 500] }, seeds: [1, 2], maxRuns: 4, budget: 12 });
  assert.equal(samples.length, 4); assert.equal(samples[0].budget, 3);
  const result = await runIterations({ maxIterations: 2, budget: 3, initial: 0, step: async ({ value }) => ({ value: value + 1, cost: 1 }), converged: value => value > 5 });
  assert.equal(result.status, 'limitReached');
  const contract = { quantity: 'force', unit: 'N', coordinateFrame: 'global', entityAssociation: 'node', timeReference: 'seconds', maxMappingError: .01 };
  assert.throws(() => validateExchange({ ...contract, unit: 'nN', values: [1], sequence: 0, mappingError: 0 }, contract), { code: 'SEMANTIC_MISMATCH' });
  assert.throws(() => validateCheckpointBundle({ time: 1, exchangeSequence: 2, checkpoints: [{ time: 0 }, { time: 1 }] }), { code: 'CHECKPOINT_INCONSISTENT' });
});
test('Red Maple public SDK executes a real isolated role session and durable operation receipts', async t => {
  const f = await fixture(t);
  const script = scripted([assistant('Role completed with actual Pi runtime')]);
  const runtime = new RedMapleRuntimePort({ store: f.store, workspace: f.root, agentDir: join(f.root, 'agent'),
    roles: { reviewer: { version: 'test/1', instructions: 'Read evidence and return references.', tools: ['read', 'grep', 'find', 'ls'] } },
    options: { model, streamFn: script.streamFn, noMcp: true, settings: { retry: { enabled: false } } } });
  const request = { role: 'reviewer', prompt: 'Return a short result' };
  const result = await runtime.executeRole(request, 'role-operation');
  assert.equal(result.status, 'succeeded');
  assert.ok(result.output.includes('actual Pi runtime'));
  assert.ok(result.sessionFile);
  assert.equal((await runtime.executeRole(request, 'role-operation')).sessionId, result.sessionId);
  await assert.rejects(runtime.executeRole({ ...request, prompt: 'different' }, 'role-operation'), { code: 'IDEMPOTENCY_CONFLICT' });
});
test('Pi writer publishes an immutable artifact through its host-scoped service tool', async t => {
  const f = await fixture(t);
  const script = scripted([assistant([call('candidate', 'simulation_artifact_publish', { name: 'candidate.in', type: 'solver-input', content: 'units metal\n' })], 'toolUse'),
    assistant('Artifact is published')]);
  const runtime = new RedMapleRuntimePort({ store: f.store, workspace: f.root, agentDir: join(f.root, 'agent'), artifacts: f.artifacts,
    roles: { writer: { instructions: 'Produce a candidate.', capabilities: ['artifact.write'], tools: ['read'] } },
    options: { model, streamFn: script.streamFn, noMcp: true } });
  const result = await runtime.executeRole({ role: 'writer', prompt: 'Publish the candidate input' }, 'writer-op');
  assert.equal(result.status, 'succeeded');
  assert.equal(result.outputRefs.length, 1);
  assert.equal((await f.artifacts.read(result.outputRefs[0])).toString(), 'units metal\n');
});

test('PDF extraction counts source text and preserves missing-page coverage', async t => {
  const f = await fixture(t);
  const pdf = join(f.root, 'paper.pdf'), text = join(f.root, 'paper.txt');
  for (const readablePages of [0, 1, 2]) {
    execFileSync('python', ['-c', 'import fitz,sys; d=fitz.open(); a=d.new_page(); b=d.new_page(); n=int(sys.argv[2]); [(d[i].insert_text((72,72),"Actual source text")) for i in range(n)]; d.save(sys.argv[1]); d.close()', pdf, String(readablePages)], { windowsHide: true });
    const result = JSON.parse(execFileSync('python', [join(simulationRoot, 'science/extract_paper.py'), pdf, text], { encoding: 'utf8', windowsHide: true }));
    assert.equal(result.readablePages, readablePages);
    assert.equal(result.representation, ['metadata', 'section', 'fullText'][readablePages]);
    assert.equal(result.characters > 0, readablePages > 0);
  }
});
