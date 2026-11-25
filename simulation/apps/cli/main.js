#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { openApplication } from '../../packages/application/index.js';
import { createGrindingPlan, runGrinding, inspectGrindingFigures } from '../../packages/application/grinding.js';
import { resolvePack } from '../../packages/pack-resolver/index.js';
import { serviceActor, requireCondition } from '../../packages/contracts/index.js';
import { simulationExtension } from '../../packages/runtime-redmaple/index.js';
import { loadSimulationRuntimeConfig } from '../../packages/runtime-redmaple/configuration.js';
import { analyzeExistingCase, visualizeExistingCase, diagnoseExistingCase, executeReviewedCase } from '../../packages/application/partial.js';
import { auditPaper } from '../../packages/application/reproduction.js';
import { runDomainInitializationCli } from '../../agent-modules/domain-initialization/cli.js';
import { withDomainInitializationPolicy } from '../../agent-modules/domain-initialization/pi-extension.js';
import { openDomainWeb } from '../domain-web/server.js';

const { positionals, values } = parseArgs({ allowPositionals: true, options: {
  workspace: { type: 'string' }, state: { type: 'string' }, validation: { type: 'boolean' },
  python: { type: 'string' }, lammps: { type: 'string' }, parameters: { type: 'string' },
  observations: { type: 'string' }, config: { type: 'string' }, help: { type: 'boolean' },
  title: { type: 'string' }, objective: { type: 'string' }, input: { type: 'string' }, file: { type: 'string' },
  'source-type': { type: 'string' }, query: { type: 'string' }, port: { type: 'string' }
} });
const [command = 'help', target] = positionals;
const root = fileURLToPath(new URL('../..', import.meta.url));
function findExecutable(name) {
  try {
    const output = execFileSync(process.platform === 'win32' ? 'where.exe' : 'which', [name], { encoding: 'utf8', windowsHide: true });
    return output.trim().split(/\r?\n/)[0];
  } catch { return undefined; }
}
let app;
try {
  if (values.help || command === 'help') {
    console.log(`Red Maple simulation

  node apps/cli/main.js plan --validation [--python PATH --lammps PATH]
  node apps/cli/main.js grinding --validation
  node apps/cli/main.js status [CASE_ID]
  node apps/cli/main.js run CASE_ID
  node apps/cli/main.js resume CASE_ID
  node apps/cli/main.js modify CASE_ID --parameters parameters.json
  node apps/cli/main.js execute CASE_ID
  node apps/cli/main.js analyze CASE_ID
  node apps/cli/main.js visualize CASE_ID
  node apps/cli/main.js diagnose CASE_ID
  node apps/cli/main.js reproduce PAPER.pdf --config runtime.json
  node apps/cli/main.js cancel CASE_ID
  node apps/cli/main.js inspect CASE_ID --observations observations.json
  node apps/cli/main.js query QUERY
  node apps/cli/main.js validate MANIFEST --validation
  node apps/cli/main.js agent --config runtime.json
  node apps/cli/main.js agent-check --config runtime.json
  node apps/cli/main.js domain-init list
  node apps/cli/main.js domain-init new --title NAME --objective TASK
  node apps/cli/main.js domain-init status LIBRARY_ID
  node apps/cli/main.js domain-init source LIBRARY_ID --file MATERIAL --source-type standard
  node apps/cli/main.js domain-init propose LIBRARY_ID --input candidate.json
  node apps/cli/main.js domain-init query LIBRARY_ID --query WORDS
  node apps/cli/main.js domain-init export LIBRARY_ID
  node apps/cli/main.js domain-web [--port 4176] [--config runtime.example.json]

Environment options --workspace ROOT --state LOCAL_DIRECTORY
Python and LAMMPS default to executables discovered in PATH.`);
  } else {
    app = await openApplication({ workspace: values.workspace || root, stateDirectory: values.state });
    const output = value => console.log(JSON.stringify(value, null, 2));
    if (command === 'domain-init') output(await runDomainInitializationCli(app.domainInitialization, {
      action: target || 'list', libraryId: positionals[2], inputFile: values.input, file: values.file,
      title: values.title, objective: values.objective, sourceType: values['source-type'], query: values.query }));
    else if (command === 'domain-web') {
      const port = values.port === undefined ? 4176 : Number(values.port);
      requireCondition(Number.isInteger(port) && port >= 0 && port <= 65535, 'SCHEMA_INVALID', 'Port must be between 0 and 65535');
      const { server, origin } = await openDomainWeb({ ...app.domainInitialization, port, configPath: values.config, python: values.python });
      console.log(`领域库工作台 ${origin}`);
      await new Promise(resolve => {
        const stop = () => server.close();
        process.on('SIGINT', stop);
        process.on('SIGTERM', stop);
        server.once('close', () => {
          process.off('SIGINT', stop);
          process.off('SIGTERM', stop);
          resolve();
        });
      });
    } else if (['plan', 'grinding'].includes(command)) {
      requireCondition(values.validation, 'AUTHORIZATION_REQUIRED', 'This draft package supports explicit candidate validation. Supply --validation.');
      const profile = JSON.parse(await readFile(values.config || join(root, 'runtime.example.json'), 'utf8')).simulation || {};
      const plan = await createGrindingPlan(app, { python: values.python || profile.python || findExecutable('python'), lammps: values.lammps || profile.lammps || findExecutable('lmp'),
        parametersFile: values.parameters, validation: true });
      output({ caseId: plan.caseId, plan: join(plan.directory, 'PLAN.json'), scheme: plan.scheme, stateDirectory: app.stateDirectory });
      if (command === 'grinding') output(await runGrinding(app, plan, { configFile: values.config, onProgress: value => console.log(JSON.stringify(value)) }));
    } else if (command === 'status') output(target ? app.store.getCase(target) : app.store.cases().map(({ id, title, status, revision }) => ({ id, title, status, revision })));
    else if (command === 'modify') {
      const original = app.store.get('grinding-plans', target);
      requireCondition(original && values.parameters, 'REFERENCE_MISSING', 'Specify an existing case and the new full parameter file');
      output(await createGrindingPlan(app, { parentCaseId: target, python: original.python, lammps: original.lammps,
        parametersFile: values.parameters, validation: true }));
    } else if (command === 'analyze') output(await analyzeExistingCase(app, target, { configFile: values.config }));
    else if (command === 'execute') output(await executeReviewedCase(app, target, { configFile: values.config, onProgress: value => console.log(JSON.stringify(value)) }));
    else if (command === 'visualize') output(await visualizeExistingCase(app, target, { configFile: values.config }));
    else if (command === 'diagnose') output(await diagnoseExistingCase(app, target));
    else if (command === 'reproduce') output(await auditPaper(app, resolve(target), await loadSimulationRuntimeConfig(values.config)));
    else if (['run', 'resume'].includes(command)) {
      const plan = app.store.get('grinding-plans', target);
      requireCondition(plan, 'REFERENCE_MISSING', 'Saved plan is missing');
      output(await runGrinding(app, plan, { configFile: values.config, onProgress: value => console.log(JSON.stringify(value)) }));
    } else if (command === 'cancel') {
      await app.kernel.command(target, 'cancel');
      for (const operation of app.store.pending(target)) output(await app.executor.cancel(operation.operationId));
      if (!app.store.pending(target).length) await app.kernel.command(target, 'cancelled');
    } else if (command === 'inspect') {
      const observations = JSON.parse(await readFile(values.observations, 'utf8'));
      output(await inspectGrindingFigures(app, target, { actor: serviceActor('image-inspection'), observations }));
    } else if (command === 'query') output({ current: await app.knowledge.search(target || '', { includeCandidates: true }),
      legacy: app.legacy && target ? app.legacy.search(target) : [] });
    else if (command === 'validate') {
      const result = await resolvePack(resolve(target), { validation: !!values.validation,
        capabilityRegistry: { 'local-lammps': { version: '0.1.0', capabilities: ['molecular.dynamics.solve'] } } });
      output({ structurallyValid: result.structurallyValid, semanticallyValid: result.semanticallyValid, executionReady: result.executionReady, diagnostics: result.diagnostics, lock: result.lock });
      if (!result.executionReady) process.exitCode = 1;
    } else if (['agent', 'agent-check'].includes(command)) {
      const config = withDomainInitializationPolicy(await loadSimulationRuntimeConfig(values.config || join(root, 'runtime.example.json')));
      const { createRedMapleRuntime } = await import('@paperpilot/agent-base');
      const runtime = await createRedMapleRuntime({ ...config, cwd: app.workspace, mode: command === 'agent' ? 'interactive' : 'print',
        extensionFactories: [simulationExtension({ ...app, profile: config.simulation || {}, runtimeOptions: config }), ...(config.extensionFactories || [])] });
      try {
        if (command === 'agent-check') {
          await runtime.session.prompt('This is a provider connectivity check. Reply with SIMULATION_PROVIDER_READY and do not call tools.');
          const message = runtime.session.messages.filter(item => item.role === 'assistant').at(-1);
          const safeReason = String(message?.errorMessage || message?.stopReason || 'No assistant result')
            .replaceAll(config.apiKey || '\u0000', '[REDACTED]');
          requireCondition(message?.stopReason === 'stop', 'TOOL_FAILED', 'Provider check did not complete successfully',
            { context: { provider: runtime.session.model.provider, model: runtime.session.model.id, reason: safeReason } });
          output({ status: 'succeeded', provider: runtime.session.model.provider, model: runtime.session.model.id,
            response: message.content.filter(item => item.type === 'text').map(item => item.text).join('\n') });
        } else {
          const { InteractiveMode } = await import('@paperpilot/agent-base/pi');
          await new InteractiveMode(runtime, {}).run();
        }
      } finally { await runtime.dispose(); }
    } else throw new Error(`Unknown command ${command}`);
  }
} catch (error) {
  console.error(JSON.stringify(error.toJSON?.() || { error: error.message, stack: error.stack }, null, 2));
  process.exitCode = 1;
} finally { app?.close(); }
