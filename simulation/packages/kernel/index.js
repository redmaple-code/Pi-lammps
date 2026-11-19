import { id, digest, evaluateCondition, topological, requireCondition } from '../contracts/index.js';
import { authorize } from './authorization.js';

export const scopes = new Set(['new', 'modify', 'execute', 'analyze', 'visualize', 'query', 'diagnose', 'resume', 'reproduce']);
export function classifyTask(task) {
  requireCondition(scopes.has(task.scope), 'SCHEMA_INVALID', 'Unsupported task scope');
  return { scope: task.scope, requiresCase: task.scope !== 'query', completedScope: task.scope };
}
export function affectedClosure(workflow, changedStages) {
  const affected = new Set(changedStages);
  for (const stage of topological(workflow.stages)) if (stage.needs.some(key => affected.has(key))) affected.add(stage.id);
  return [...affected];
}
export class Kernel {
  constructor(store, gates) { Object.assign(this, { store, gates }); }
  async command(caseId, action, payload = {}, { expectedRevision, commandId = id('command') } = {}) {
    const current = this.store.getCase(caseId);
    const expected = expectedRevision ?? current.revision;
    let review;
    if (action === 'review') review = await this.gates.validate(payload.receiptRef, current, current.stages.find(s => s.id === payload.stageId));
    return this.store.dispatch(caseId, { id: commandId, action, payload }, expected, state => {
      const stage = payload.stageId && state.stages.find(s => s.id === payload.stageId);
      const events = [], intents = [];
      if (payload.stageId) requireCondition(stage, 'REFERENCE_MISSING', 'Unknown stage');
      if (action === 'start') {
        requireCondition(['pending', 'needsRevision', 'blocked', 'failed'].includes(stage.status), 'STATE_CONFLICT', 'Stage cannot start');
        requireCondition(stage.consecutiveRevisions < state.lock.policies.maxConsecutiveRevisions, 'REVISION_LIMIT', 'Stage revision limit reached');
        for (const key of stage.needs) requireCondition(['passed', 'skipped'].includes(state.stages.find(s => s.id === key)?.status), 'DEPENDENCY_PENDING', `Dependency ${key} is not resolved`);
        const condition = evaluateCondition(stage.when, payload.facts || {});
        requireCondition(condition === true, 'CONDITION_UNKNOWN', 'Stage condition is not true');
        requireCondition(state.status === 'active', 'STATE_CONFLICT', 'Case is not active');
        stage.status = 'running'; stage.attempt += 1; stage.producerInstanceId = payload.producerInstanceId;
        stage.inputRefs = payload.inputRefs || [];
        if (payload.request) {
          const operationId = payload.operationId || id('operation');
          stage.operationId = operationId;
          intents.push({ operationId, request: payload.request, stageId: stage.id, attempt: stage.attempt });
        }
      } else if (action === 'attachOperation') {
        requireCondition(stage.status === 'running' && !stage.operationId && payload.request && payload.operationId,
          'STATE_CONFLICT', 'Only a running stage without an operation can attach a request');
        stage.operationId = payload.operationId;
        intents.push({ operationId: payload.operationId, request: payload.request, stageId: stage.id, attempt: stage.attempt });
      } else if (action === 'produced') {
        requireCondition(stage.status === 'running', 'STATE_CONFLICT', 'Stage is not running');
        stage.outputRefs = payload.outputRefs || [];
        stage.outputSlots = payload.outputSlots || {};
        requireCondition(stage.outputRefs.length > 0, 'EVIDENCE_INSUFFICIENT', 'Stage has no output artifacts');
        if (stage.operationId) requireCondition(this.store.operation(stage.operationId)?.status === 'succeeded', 'RESULT_UNKNOWN', 'Operation is not confirmed');
        stage.status = stage.reviewRequired ? 'awaitingReview' : 'passed';
        if (!stage.reviewRequired) requireCondition(payload.completionReceiptRef && this.store.get('completions', payload.completionReceiptRef), 'EVIDENCE_INSUFFICIENT', 'Program completion receipt required');
      } else if (action === 'awaitInspection') {
        requireCondition(stage.status === 'running' && payload.outputRefs?.length, 'STATE_CONFLICT', 'Inspection needs running stage outputs');
        stage.outputRefs = payload.outputRefs;
        stage.status = 'awaitingInspection';
      } else if (action === 'recoverProduced') {
        const proof = this.store.get('role-operations', payload.roleOperationId);
        requireCondition(stage.status === 'running' && stage.operationId && this.store.operation(stage.operationId)?.status === 'succeeded',
          'RESULT_UNKNOWN', 'Recovered production requires a confirmed external operation');
        requireCondition(proof?.status === 'succeeded' && proof.caseId === state.id && proof.revisionId === state.revisionId
          && payload.outputRefs?.length, 'EVIDENCE_INSUFFICIENT', 'Recovered production requires its successful role receipt and outputs');
        stage.recoveredProducerInstanceId = stage.producerInstanceId;
        stage.producerInstanceId = proof.actor.actorInstanceId;
        stage.outputRefs = payload.outputRefs;
        stage.outputSlots = payload.outputSlots;
        stage.status = stage.reviewRequired ? 'awaitingReview' : 'passed';
      } else if (action === 'inspected') {
        requireCondition(stage.status === 'awaitingInspection', 'STATE_CONFLICT', 'Stage is not awaiting image inspection');
        requireCondition(payload.inspectionRef && this.store.get('completions', payload.inspectionRef), 'EVIDENCE_INSUFFICIENT', 'Inspection receipt is required');
        stage.outputRefs = payload.outputRefs;
        stage.outputSlots = { [stage.produces?.[0] || `${stage.id}-output`]: payload.outputRefs };
        stage.status = 'passed';
      } else if (action === 'bindReviewSources') {
        requireCondition(stage.status === 'awaitingReview' && Array.isArray(payload.sources)
          && payload.sources.every(source => source.sourceId && /^sha256:[a-f0-9]{64}$/.test(source.digest)),
        'SCHEMA_INVALID', 'Review source binding needs current source identities and digests');
        stage.evidencePolicy = { ...stage.evidencePolicy, claims: (stage.evidencePolicy?.claims || []).map(claim => {
          const sourceDigests = payload.sources.filter(source => claim.sourceIds.includes(source.sourceId)).map(source => source.digest);
          requireCondition(claim.blocking === false || sourceDigests.length > 0, 'EVIDENCE_INSUFFICIENT', 'Required claim has no registered source');
          return { ...claim, sourceDigests };
        }) };
      } else if (action === 'review') {
        requireCondition(stage.status === 'awaitingReview', 'STATE_CONFLICT', 'Stage is not awaiting review');
        stage.receipts.push(review.id);
        if (review.verdict === 'PASS') { stage.status = 'passed'; stage.consecutiveRevisions = 0; }
        else if (review.verdict === 'REVISE') {
          stage.consecutiveRevisions += 1;
          stage.status = stage.consecutiveRevisions >= state.lock.policies.maxConsecutiveRevisions ? 'blocked' : 'needsRevision';
        } else { stage.status = 'blocked'; stage.blockedReason = 'reviewBlocked'; }
      } else if (action === 'skip') {
        requireCondition(['pending', 'blocked'].includes(stage.status) && evaluateCondition(stage.when, payload.facts) === false, 'CONDITION_UNKNOWN', 'Skipping needs false applicability evidence');
        stage.status = 'skipped'; stage.skipFacts = payload.facts;
      } else if (action === 'fail') {
        requireCondition(['running', 'awaitingReview'].includes(stage.status), 'STATE_CONFLICT', 'Cannot fail this stage');
        stage.status = payload.unknown ? 'unknown' : 'failed'; stage.failureRef = payload.failureRef;
      } else if (action === 'block') {
        requireCondition(['pending', 'ready', 'running', 'blocked'].includes(stage.status), 'STATE_CONFLICT', 'Cannot block terminal stage');
        stage.status = 'blocked'; stage.blockedReason = payload.reason;
      } else if (action === 'reconcile') {
        requireCondition(stage.status === 'unknown', 'STATE_CONFLICT', 'Stage is not unknown');
        const operation = this.store.operation(stage.operationId);
        requireCondition(['succeeded', 'failed', 'cancelled'].includes(operation?.status), 'RESULT_UNKNOWN', 'External outcome is not known');
        stage.status = operation.status === 'succeeded' ? 'running' : operation.status;
      } else if (action === 'invalidate') {
        const affected = affectedClosure(state.workflow, payload.changedStages);
        state.invalidations.push({ id: id('invalidation'), affected, reason: payload.reason, history: structuredClone(state.stages.filter(s => affected.includes(s.id))) });
        for (const item of state.stages.filter(s => affected.includes(s.id))) {
          requireCondition(!['running', 'unknown', 'cancelRequested'].includes(item.status), 'RESULT_UNKNOWN', 'Reconcile active operations before invalidation');
          item.status = 'pending'; item.outputRefs = []; item.inputRefs = []; item.outputSlots = {}; delete item.operationId;
        }
        state.status = 'active';
      } else if (action === 'cancel') {
        state.status = 'cancelRequested';
        for (const item of state.stages) if (!['passed', 'skipped', 'cancelled'].includes(item.status)) item.status = 'cancelRequested';
      } else if (action === 'cancelled') {
        requireCondition(!this.store.pending(caseId).length, 'RESULT_UNKNOWN', 'External operations still pending');
        state.status = 'cancelled';
        for (const item of state.stages) if (item.status === 'cancelRequested') item.status = 'cancelled';
      } else if (action === 'repair') {
        const chain = state.repairs[payload.chainId] || { id: payload.chainId, failures: 0, history: [] };
        requireCondition(chain.failures < state.lock.policies.maxConsecutiveRepairFailures, 'REPAIR_LIMIT', 'Repair limit reached');
        chain.history.push(payload);
        chain.failures = payload.success ? 0 : chain.failures + 1;
        state.repairs[chain.id] = chain;
      } else throw new Error(`Unsupported kernel command ${action}`);
      if (state.status === 'active' && state.stages.every(s => ['passed', 'skipped'].includes(s.status))) state.status = 'completed';
      events.push({ kind: action, payload: { ...payload, stageStatus: stage?.status || null } });
      return { events, intents };
    });
  }
  permit(caseId, { closure, authorizationRef, resources, capability = 'solve' }) {
    const state = this.store.getCase(caseId);
    requireCondition(['active', 'completed'].includes(state.status), 'STATE_CONFLICT', 'Case cannot authorize execution in its current state');
    this.validateClosure(closure);
    authorize(this.store, authorizationRef, capability, { caseId, resources });
    const required = state.stages.filter(stage => stage.requiredForExecution);
    requireCondition(required.length > 0 && required.every(stage => stage.status === 'passed'), 'REVIEW_REQUIRED', 'Execution preconditions are not passed');
    const approvedRefs = required.flatMap(stage => stage.outputRefs);
    requireCondition(closure.entries.every(({ ref }) => approvedRefs.some(approved => approved.digest === ref.digest && approved.byteLength === ref.byteLength)),
      'REVIEW_REQUIRED', 'Execution closure contains an input outside the passed stages');
    const permit = { id: id('permit'), caseId, revisionId: state.revisionId, stateRevision: state.revision,
      lockRef: state.lock.digest, closureDigest: closure.digest, requiredStages: required.map(s => ({ id: s.id, attempt: s.attempt, receipts: s.receipts })),
      authorizationRef, resources, capability };
    this.store.put('permits', permit.id, permit, 0);
    return permit;
  }
  validatePermit(permitRef, closure) {
    this.validateClosure(closure);
    const permit = this.store.get('permits', permitRef), state = permit && this.store.getCase(permit.caseId);
    requireCondition(permit && state.revisionId === permit.revisionId && state.lock.digest === permit.lockRef && closure.digest === permit.closureDigest,
      'ARTIFACT_CHANGED', 'Execution permit is stale');
    requireCondition(['active', 'completed'].includes(state.status), 'STATE_CONFLICT', 'Case execution was cancelled');
    for (const required of permit.requiredStages) {
      const stage = state.stages.find(s => s.id === required.id);
      requireCondition(stage?.status === 'passed' && stage.attempt === required.attempt && digest(stage.receipts) === digest(required.receipts), 'REVIEW_REQUIRED', 'Required stage changed');
    }
    authorize(this.store, permit.authorizationRef, permit.capability, { caseId: permit.caseId, resources: permit.resources });
    return permit;
  }
  validateClosure(closure) {
    requireCondition(closure?.algorithm === 'closure-json-v1' && closure.entries?.length > 0
      && closure.digest === digest(closure.entries.map(({ path, ref }) => ({ path, digest: ref.digest, byteLength: ref.byteLength }))),
    'ARTIFACT_CHANGED', 'Execution closure content differs from its digest');
  }
}
