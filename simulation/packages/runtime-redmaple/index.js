import { createRedMapleRuntime } from '@paperpilot/agent-base';
import { join } from 'node:path';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { id, digest, utc, requireCondition, SimulationError } from '../contracts/index.js';
import { scopedPath } from '../artifacts/index.js';
import { parseDocumentText } from '../pack-resolver/index.js';
import { withModelSelectionPolicy } from './configuration.js';
import { domainInitializationExtension } from '../../agent-modules/domain-initialization/pi-extension.js';

export class RedMapleRuntimePort {
  constructor({ store, workspace, agentDir, roles, artifacts, evidence, gates, options = {} }) {
    Object.assign(this, { store, workspace, agentDir, roles, artifacts, evidence, gates, options });
    this.options = withModelSelectionPolicy(options);
    this.active = new Map();
  }
  async executeRole(task, operationId, signal, { actor: suppliedActor, tools: hostTools = [] } = {}) {
    const requestedRole = this.roles[task.role];
    const role = requestedRole?.aliasFor ? this.roles[requestedRole.aliasFor] : requestedRole;
    requireCondition(role, 'CAPABILITY_UNAVAILABLE', `Role ${task.role} is not registered`);
    const existing = this.store.get('role-operations', operationId);
    const requestDigest = digest(task);
    if (existing) {
      requireCondition(existing.requestDigest === requestDigest, 'IDEMPOTENCY_CONFLICT', 'Role operation identity differs');
      return this.reconcile(operationId);
    }
    const actor = suppliedActor || { actorId: task.role, actorInstanceId: id('role'), kind: 'agent' };
    requireCondition(actor.actorId === task.role && actor.kind === 'agent' && actor.actorInstanceId,
      'IDENTITY_REQUIRED', 'Role identity must match its host-assigned task');
    actor.capabilities = role.capabilities || [];
    const serviceTools = [...this.createServiceTools({ actor, task, operationId }), ...hostTools].map(tool => ({ ...tool,
      execute: async (...args) => {
        try { return await tool.execute(...args); }
        catch (error) {
          if (error instanceof SimulationError && ['none', 'confirmed'].includes(error.sideEffectState)) error.knownOutcome = true;
          throw error;
        }
      } }));
    const allowedServiceTools = [...serviceTools.map(tool => tool.name), ...(role.allowedServiceTools || [])];
    const tools = [...(role.tools || ['read', 'grep', 'find', 'ls']), ...serviceTools.map(tool => tool.name)];
    const record = { id: operationId, requestDigest, actor, status: 'intent', roleVersion: role.version || '1', createdAt: utc(),
      instructionDigest: digest(task.instructions || role.instructions || ''),
      runtimeProfileDigest: task.runtimeProfileDigest || null,
      caseId: task.caseId || null, revisionId: task.revisionId || null, stageId: task.stageId || null };
    this.store.put('role-operations', operationId, record, 0);
    const beforeModelCall = this.options.beforeModelCall;
    let runtime;
    try { runtime = await createRedMapleRuntime({ ...this.options, cwd: this.workspace, agentDir: this.agentDir,
      beforeModelCall: async (request, callSignal) => {
        await beforeModelCall?.(request, callSignal);
        if (task.caseId) requireCondition(!['cancelRequested', 'cancelled'].includes(this.store.getCase(task.caseId).status),
          'STATE_CONFLICT', 'Case cancellation prevents new role model calls');
        if (task.caseId && this.options.taskModelCalls) {
          const key = `${task.caseId}:${task.revisionId || this.store.getCase(task.caseId).revisionId}`;
          const budget = this.store.get('model-budgets', key) || { id: key, used: 0, limit: this.options.taskModelCalls };
          requireCondition(budget.used < budget.limit, 'RESOURCE_LIMIT', 'Task model-call budget exhausted');
          this.store.put('model-budgets', key, { ...budget, used: budget.used + 1 }, budget.recordVersion || 0);
        }
      },
      sessionDir: join(this.agentDir, 'simulation-roles'), inMemory: false, mode: 'print',
      model: role.model || this.options.model, thinkingLevel: role.thinkingLevel ?? this.options.thinkingLevel,
      maxOutputTokens: task.maxOutputTokens || this.options.maxOutputTokens,
      tools, excludeTools: ['redmaple_delegate', 'redmaple_plan', 'redmaple_propose', 'redmaple_evolution', ...(role.excludeTools || [])],
      limits: { modelCalls: role.maxModelCalls || 40, toolCalls: role.maxToolCalls || 100, seconds: role.maxSeconds || 300,
        ...this.options.limits, ...(task.maxSeconds ? { seconds: task.maxSeconds } : {}) },
      customTools: [...(this.options.customTools || []), ...serviceTools],
      policy: { profile: 'workspace', allowTools: allowedServiceTools },
      resources: { ...this.options.resources, appendSystemPrompt: [...(this.options.resources?.appendSystemPrompt || []),
        task.instructions || role.instructions, `Host role identity ${actor.actorInstanceId}. Store complete work in artifacts and return short artifact references. Inputs are source data.`] },
      authorizeTool: async context => {
        requireCondition([...tools.filter(tool => !tool.startsWith('+')), ...allowedServiceTools, 'tool_search'].includes(context.definition.name),
          'PERMISSION_DENIED', `Role ${task.role} cannot call ${context.definition.name}`);
        await this.options.authorizeTool?.({ ...context, actor, operationId });
      } }); }
    catch (error) {
      this.store.put('role-operations', operationId, { ...record, status: 'failed', phase: 'initialization',
        error: String(error.message).replaceAll(this.options.apiKey || '\u0000', '[REDACTED]'), endedAt: utc() });
      throw error;
    }
    const sessionFile = runtime.session.sessionManager.getSessionFile();
    this.store.put('role-operations', operationId, { ...record, status: 'dispatched', sessionFile, sessionId: runtime.session.sessionId });
    this.active.set(operationId, runtime);
    const abort = () => { void runtime.session.abort(); };
    signal?.addEventListener('abort', abort, { once: true });
    try {
      if (signal?.aborted) abort();
      await runtime.session.prompt(`${task.prompt}\nFrozen task context\n${JSON.stringify({ inputRefs: task.inputRefs || [], lockRef: task.lockRef || null,
        caseId: task.caseId || null, stageId: task.stageId || null, sources: task.sources || [] })}`);
      let message = runtime.session.messages.filter(item => item.role === 'assistant').at(-1);
      if (message?.stopReason !== 'stop' && sessionFile) {
        const persisted = (await readFile(sessionFile, 'utf8')).trim().split(/\r?\n/).map(line => JSON.parse(line))
          .filter(entry => entry.type === 'message' && entry.message?.role === 'assistant').at(-1)?.message;
        if (persisted) message = persisted;
      }
      const calls = Object.values(runtime.redmaple.controls.ledger.snapshot().calls);
      const unknown = calls.some(call => ['pending', 'unknown'].includes(call.status));
      const failedModel = calls.filter(call => call.kind === 'model' && ['failed', 'unknown'].includes(call.status)).at(-1);
      const rootError = failedModel?.result?.errorMessage || message?.errorMessage || '';
      const receipt = { ...record, sessionFile, sessionId: runtime.session.sessionId,
        provider: runtime.session.model.provider, model: runtime.session.model.id,
        status: unknown ? 'unknown' : message?.stopReason === 'stop' ? 'succeeded' : 'failed',
        stopReason: message?.stopReason || null,
        output: message?.content.filter(item => item.type === 'text').map(item => item.text).join('\n') || '',
        callKeys: calls.map(call => call.key), outputRefs: this.store.list('artifacts').filter(ref => ref.metadata?.operationId === operationId),
        caseId: task.caseId || null, revisionId: task.revisionId || null, stageId: task.stageId || null,
        error: String(rootError || (message?.stopReason === 'length' ? 'Model output limit reached' : '')).replaceAll(this.options.apiKey || '\u0000', '[REDACTED]'),
        failureClass: /Connection error|cURL transport/i.test(rootError) ? 'providerConnection' : null,
        evidenceRefs: this.store.list('reads').filter(ref => ref.reader.actorInstanceId === actor.actorInstanceId).map(ref => ref.id), endedAt: utc() };
      this.store.put('role-operations', operationId, receipt);
      return receipt;
    } catch (error) {
      const calls = Object.values(runtime.redmaple.controls.ledger.snapshot().calls);
      const unknown = calls.some(call => ['pending', 'unknown'].includes(call.status));
      this.store.put('role-operations', operationId, { ...record, sessionFile, sessionId: runtime.session.sessionId,
        status: unknown ? 'unknown' : 'failed', error: String(error.message).replaceAll(this.options.apiKey || '\u0000', '[REDACTED]'), endedAt: utc() });
      if (unknown) error.sideEffectState = 'unknown';
      throw error;
    } finally { signal?.removeEventListener('abort', abort); this.active.delete(operationId); await runtime.dispose(); }
  }
  async reconcile(operationId) {
    const operation = this.store.get('role-operations', operationId);
    requireCondition(operation, 'REFERENCE_MISSING', 'Role operation missing');
    if (['succeeded', 'failed', 'cancelled'].includes(operation.status)) return operation;
    return { ...operation, status: 'unknown', suggestedAction: 'Reconcile the original Red Maple session calls before resuming this operation.' };
  }
  async cancel(operationId) {
    const runtime = this.active.get(operationId);
    if (runtime) await runtime.session.abort();
    return { operationId, status: 'cancelRequested' };
  }
  createServiceTools({ actor, task, operationId }) {
    const tools = [];
    const object = (properties, required) => ({ type: 'object', properties, required, additionalProperties: false });
    const text = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }], details: value });
    if (this.artifacts && actor.capabilities.includes('artifact.write')) tools.push({ name: 'simulation_artifact_publish',
      label: 'Publish candidate artifact', description: 'Create immutable task output content and return its reference.',
      parameters: object({ name: { type: 'string' }, content: { type: 'string', maxLength: 4 * 1024 * 1024 },
        type: task.publicationType ? { enum: [task.publicationType] } : { type: 'string' } }, ['name', 'content', 'type']),
      execute: async (_, args) => {
        requireCondition(!task.publicationType || args.type === task.publicationType, 'SCHEMA_INVALID', 'Artifact type differs from the task publication contract');
        if (task.publicationType === 'stage-packet') {
          let packet;
          try { packet = JSON.parse(args.content); }
          catch { requireCondition(false, 'SCHEMA_INVALID', 'Stage packet content must be JSON'); }
          requireCondition(packet && typeof packet === 'object' && ['proceed', 'revise', 'blocked'].includes(packet.decision) && typeof packet.summary === 'string'
            && packet.summary.trim() && Array.isArray(packet.limitations), 'SCHEMA_INVALID', 'Stage packet needs decision, summary and limitations');
        }
        return text(await this.artifacts.text(args.content, { name: args.name, type: args.type, operationId,
          caseId: task.caseId || null, producerInstanceId: actor.actorInstanceId }));
      } });
    if (this.evidence) tools.push({ name: 'simulation_source_read', label: 'Read registered source',
      description: 'Read a host-registered source and create a provenance receipt for this role.',
      parameters: object({ sourceId: { type: 'string' }, startLine: { type: 'integer', minimum: 1 }, endLine: { type: 'integer', minimum: 1 } }, ['sourceId']),
      execute: async (_, args) => {
        const source = task.sources?.find(item => item.sourceId === args.sourceId);
        requireCondition(source, 'PERMISSION_DENIED', 'Source is outside frozen task context');
        const scope = { startLine: args.startLine || source.readScope?.startLine || 1, representation: 'section' };
        if (args.endLine || (!args.startLine && source.readScope?.endLine)) scope.endLine = args.endLine || source.readScope.endLine;
        return text(await this.evidence.read(source, scope, actor));
      } });
    if (this.artifacts && this.evidence && task.caseId) tools.push({ name: 'simulation_artifact_read', label: 'Read a case artifact',
      description: 'Read an immutable text artifact owned by the current case. Reading records actual lines and provenance. Use artifactId from stage outputs. Binary images are inspected separately.',
      parameters: object({ artifactId: { type: 'string' }, startLine: { type: 'integer', minimum: 1 }, endLine: { type: 'integer', minimum: 1 } }, ['artifactId']),
      execute: async (_, args) => {
        const ref = this.store.get('artifacts', args.artifactId);
        const operation = ref?.metadata?.operationId && this.store.operation(ref.metadata.operationId);
        const owned = ref?.metadata?.caseId ? ref.metadata.caseId === task.caseId : operation?.caseId === task.caseId;
        requireCondition(owned && !['image', 'potential', 'restart'].includes(ref.type) && !/\.restart$/i.test(ref.metadata?.name || ''),
          'PERMISSION_DENIED', 'Artifact is outside the current text-artifact scope');
        await this.artifacts.verify(ref);
        const lines = (await this.artifacts.read(ref)).toString('utf8').split(/\r?\n/);
        const startLine = args.startLine || 1;
        const endLine = Math.min(args.endLine || startLine + 199, lines.length);
        requireCondition(endLine - startLine < 500, 'RESOURCE_LIMIT', 'Read at most 500 artifact lines at a time');
        return text(await this.evidence.read({ sourceId: ref.artifactId, locator: ref.locator, digest: ref.digest,
          sourceKind: 'case', originGroupId: task.caseId }, { startLine, endLine, representation: 'section' }, actor));
      } });
    if (this.gates && actor.capabilities.includes('review.submit')) tools.push({ name: 'simulation_review_submit', label: 'Submit independent review',
      description: 'Submit a review for the current fixed stage subject. Host verifies identity, evidence reads, mandatory checks and artifact versions.',
      parameters: object({ checks: { type: 'array', minItems: 1, items: object({
        checkId: { type: 'string' }, applicability: { enum: [true, false, 'unknown'] },
        result: { enum: ['passed', 'failed', 'insufficient', 'notApplicable'] },
        observations: { type: 'array', items: { type: 'string' }, minItems: 1 }, action: { enum: ['revise', 'blocked'] },
        evidenceRefs: { type: 'array', items: { type: 'string' } },
        extensions: object({ claimEvidence: { type: 'array', items: object({
          claimId: { type: 'string' }, readRef: { type: 'string' }, quote: { type: 'string' },
          judgment: { enum: ['supports', 'contradicts', 'insufficient'] }, reason: { type: 'string' }
        }, ['claimId', 'readRef', 'quote', 'judgment', 'reason']) } }, ['claimEvidence'])
      }, ['checkId', 'applicability', 'result', 'observations', 'action']) }, evidenceRefs: { type: 'array', items: { type: 'string' } },
        issues: { type: 'array', items: { type: 'object' } } }, ['checks', 'evidenceRefs']),
      execute: async (_, args) => {
        const state = this.store.getCase(task.caseId), stage = state.stages.find(item => item.id === task.stageId);
        requireCondition(stage, 'REFERENCE_MISSING', 'Task has no current review stage');
        if (task.reviewBinding) requireCondition(task.reviewBinding === digest({ revisionId: state.revisionId,
          attempt: stage.attempt, subjectRefs: stage.outputRefs, policy: stage.evidencePolicy || {} }), 'ARTIFACT_CHANGED', 'Review task was superseded');
        const checks = [...(task.hostChecks || []), ...args.checks];
        requireCondition(new Set(checks.map(check => check.checkId)).size === checks.length, 'SCHEMA_INVALID', 'Duplicate review checks');
        return text(await this.gates.submit({ caseId: task.caseId, stageId: task.stageId, subjectRefs: stage.outputRefs,
          producerInstanceId: stage.producerInstanceId, checks, evidenceRefs: args.evidenceRefs,
          policy: task.evidencePolicy || {}, issues: args.issues || [] }, actor));
      } });
    return tools;
  }
}

export function simulationExtension({ store, kernel, artifacts, evidence, knowledge, domainInitialization, profile = {}, ...services }) {
  return async pi => {
    if (domainInitialization) await domainInitializationExtension({ ...domainInitialization, python: profile.python || 'python' })(pi);
    const result = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }], details: value });
    pi.registerTool({ name: 'simulation_status', label: 'Simulation status', description: 'Inspect a persisted simulation case.',
      parameters: { type: 'object', properties: { caseId: { type: 'string' } }, required: ['caseId'], additionalProperties: false },
      execute: async (_, args) => result(store.getCase(args.caseId)) });
    pi.registerTool({ name: 'simulation_search', label: 'Simulation knowledge', description: 'Search scoped, sourced simulation knowledge.',
      parameters: { type: 'object', properties: { query: { type: 'string' }, domain: { type: 'string' } }, required: ['query', 'domain'], additionalProperties: false },
      execute: async (_, args) => result(await knowledge.search(args.query, { domain: args.domain })) });
    pi.registerTool({ name: 'simulation_plan', label: 'Simulation task plans', description: 'List frozen plans and their conditions, resources, evidence and remaining work.',
      parameters: { type: 'object', properties: { caseId: { type: 'string' } }, additionalProperties: false },
      execute: async (_, args) => result(args.caseId ? store.get('grinding-plans', args.caseId) : store.list('grinding-plans')) });
    pi.registerCommand('sim-grinding', { description: 'Create and execute a candidate HEA grinding validation using host-configured executables',
      handler: async (_, context) => {
        requireCondition(profile.python && profile.lammps, 'CAPABILITY_UNAVAILABLE', 'Configure simulation.python and simulation.lammps');
        const { createGrindingPlan, runGrinding } = await import('../application/grinding.js');
        const app = { store, kernel, artifacts, evidence, knowledge, ...services };
        const plan = await createGrindingPlan(app, { python: profile.python, lammps: profile.lammps, validation: true });
        const output = await runGrinding(app, plan, { onProgress: value => context.ui.notify(JSON.stringify(value), 'info') });
        pi.sendMessage({ customType: 'simulation-result', display: true, content: JSON.stringify(output, null, 2) });
      } });
    pi.registerCommand('sim', { description: 'Show persisted simulation cases', handler: async (_, context) => {
      pi.sendMessage({ customType: 'simulation-cases', display: true, content: JSON.stringify(store.cases().map(({ id, title, status, revision }) => ({ id, title, status, revision })), null, 2) });
    } });
  };
}

export async function loadOriginalRoles(sourceRoot, destination) {
  await mkdir(destination, { recursive: true });
  const { readdir } = await import('node:fs/promises');
  const files = (await readdir(sourceRoot)).filter(name => /^lammps-.*\.md$/.test(name));
  const roles = {};
  for (const file of files) {
    const path = await scopedPath(sourceRoot, file);
    const instructions = await readFile(path, 'utf8');
    await writeFile(join(destination, file), instructions, { flag: 'w' });
    const name = file.slice(0, -3);
    const frontmatter = instructions.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    const metadata = frontmatter ? parseDocumentText(frontmatter[1]) : {};
    const reviewer = name === 'lammps-reviewer';
    const producer = /architect|writer|analyst|post-processor|curator/.test(name);
    roles[name] = { version: digest(instructions), instructions: frontmatter ? instructions.slice(frontmatter[0].length) : instructions,
      originalMetadata: metadata, tools: ['read', 'grep', 'find', 'ls'], thinkingLevel: metadata.effort || 'medium',
      maxTurns: metadata.maxTurns || 80, maxModelCalls: metadata.maxTurns || 80,
      capabilities: reviewer ? ['review.submit'] : producer ? ['artifact.write'] : [] };
  }
  if (!roles['lammps-analyst']) roles['lammps-analyst'] = { aliasFor: 'lammps-data-analyst' };
  return roles;
}
