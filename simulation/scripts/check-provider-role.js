import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { openApplication } from '../packages/application/index.js';
import { RedMapleRuntimePort } from '../packages/runtime-redmaple/index.js';
import { loadSimulationRuntimeConfig } from '../packages/runtime-redmaple/configuration.js';
import { id, requireCondition } from '../packages/contracts/index.js';
import { simulationRoot } from './build-packs.js';
import { assessCommandAnswers } from './assess-command-answers.js';

const config = await loadSimulationRuntimeConfig(join(simulationRoot, 'runtime.example.json'));
const app = await openApplication({ workspace: simulationRoot });
const operationId = id('provider-role-check');
const directory = join(simulationRoot, 'work/provider-checks', operationId);
try {
  await mkdir(directory, { recursive: true });
  const source = join(directory, 'commands.txt');
  await writeFile(source, [
    'LAMMPS command interpretation exercise. Return a concise JSON artifact with reasons.',
    'units metal', 'timestep 0.001',
    'variable xtip equal "17.8+(step-4000)*dt*10"',
    'variable current_step equal step', 'variable current_time equal time',
    'fix sampled all print 1 "${current_step} ${current_time}" file variable-print.csv screen no',
    'variable saved_length equal $(lx)', 'variable L0 equal ${saved_length}',
    'variable strain equal "(lx-v_L0)/v_L0"',
    'read_data alloy.data', 'pair_style eam/alloy', 'pair_coeff * * FeNiCrCoCu-heafixed.setfl Fe Ni Cr Co Cu',
    'Questions are independent snippets. Assess each in its valid command context.',
    'What is the tip speed in m/s?',
    'Does the double-quoted fix print expression support repeated variable evaluation?',
    'Can this strain expression correctly use current lx and a frozen L0?',
    'Can an EAM setfl load masses that differ from an earlier read_data mass table?'
  ].join('\n'));
  const runtime = new RedMapleRuntimePort({ ...app, agentDir: join(app.stateDirectory, 'provider-check-agents'),
    roles: { commandReviewer: { instructions: 'Read the registered source using simulation_source_read, then publish one JSON artifact using simulation_artifact_publish. Answer the command questions independently. Do not run commands or read other files.',
      tools: ['read'], capabilities: ['artifact.write'] } },
    options: { ...config, limits: { modelCalls: 8, toolCalls: 20, seconds: 180 } } });
  const result = await runtime.executeRole({ role: 'commandReviewer',
    prompt: 'Read source command-semantics, then publish a JSON artifact with speedMPerS (number), fixPrintVariablesValid (boolean), engineeringStrainValid (boolean), eamCanSetMasses (boolean), and reasons. Return the artifact reference.',
    sources: [{ sourceId: 'command-semantics', locator: source, sourceKind: 'manual-exercise', originGroupId: operationId }] }, operationId);
  let findings;
  for (const ref of result.outputRefs || []) {
    try { findings = JSON.parse((await app.artifacts.read(ref)).toString('utf8')); break; }
    catch (error) { if (!(error instanceof SyntaxError)) throw error; }
  }
  const assessment = assessCommandAnswers(findings);
  const checks = { succeeded: result.status === 'succeeded', sourceRead: result.evidenceRefs?.length > 0,
    artifactPublished: !!findings, ...assessment.checks };
  const report = { operationId, provider: result.provider, model: result.model, checks, findings, assessment,
    sessionId: result.sessionId, outputRefs: result.outputRefs, evidenceRefs: result.evidenceRefs,
    scope: 'Real provider single-role tool/read/publish check against independent LAMMPS command probes' };
  await writeFile(join(directory, 'result.json'), JSON.stringify(report, null, 2));
  app.store.put('provider-checks', operationId, report, 0);
  requireCondition(Object.values(checks).every(Boolean), 'PROVIDER_CHECK_FAILED', 'Provider role check has unresolved findings',
    { context: { operationId, checks } });
  console.log(JSON.stringify({ status: 'succeeded', provider: result.provider, model: result.model, operationId, checks, directory }, null, 2));
} finally { app.close(); }
