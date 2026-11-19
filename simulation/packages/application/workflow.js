import { id, evaluateCondition, requireCondition, topological } from '../contracts/index.js';

export class WorkflowRunner {
  constructor({ app, handlers, reviewers = {}, facts = async () => ({}), afterStage }) { Object.assign(this, { app, handlers, reviewers, facts, afterStage }); }
  async run(caseId, { signal, onProgress } = {}) {
    const { store, kernel } = this.app;
    for (const node of topological(store.getCase(caseId).workflow.stages)) {
      if (signal?.aborted) { await kernel.command(caseId, 'cancel'); return store.getCase(caseId); }
      let state = store.getCase(caseId), stage = state.stages.find(item => item.id === node.id);
      if (['passed', 'skipped'].includes(stage.status)) continue;
      if (['unknown', 'cancelRequested', 'cancelled', 'awaitingInspection'].includes(stage.status)) return state;
      if (stage.status === 'blocked' && stage.blockedReason === 'reviewBlocked') return state;
      if (stage.status !== 'awaitingReview') {
        const facts = await this.facts({ state, stage });
        const when = evaluateCondition(stage.when, facts);
        if (when === false) { await kernel.command(caseId, 'skip', { stageId: stage.id, facts }); continue; }
        if (when === 'unknown') { await kernel.command(caseId, 'block', { stageId: stage.id, reason: 'CONDITION_UNKNOWN' }); return store.getCase(caseId); }
        const slots = { ...state.externalInputs };
        for (const predecessor of state.stages.filter(item => item.status === 'passed')) Object.assign(slots, predecessor.outputSlots);
        const missing = (stage.consumes || []).filter(slot => !slots[slot]);
        if (missing.length) { await kernel.command(caseId, 'block', { stageId: stage.id, reason: `Missing slots ${missing.join(', ')}` }); return store.getCase(caseId); }
        const handler = this.handlers[stage.id] || this.handlers[stage.role] || this.handlers[stage.capability];
        requireCondition(handler, 'CAPABILITY_UNAVAILABLE', `No trusted handler for ${stage.capability || stage.role}`);
        requireCondition(stage.status !== 'running', 'RESULT_UNKNOWN', 'Recover the original handler operation before replaying a running stage');
        const actor = handler.createActor?.({ state, stage }) || handler.actor;
        requireCondition(actor?.actorInstanceId, 'IDENTITY_REQUIRED', 'Stage handler needs a host identity');
        await kernel.command(caseId, 'start', { stageId: stage.id, facts, producerInstanceId: actor.actorInstanceId,
          inputRefs: (stage.consumes || []).flatMap(slot => slots[slot]) });
        onProgress?.({ caseId, stageId: stage.id, status: 'running' });
        try {
          const output = await handler.execute({ state: store.getCase(caseId), stage: store.getCase(caseId).stages.find(item => item.id === stage.id), inputs: slots, actor, signal });
          if (output?.paused) return store.getCase(caseId);
          const outputRefs = Object.values(output.outputSlots).flat();
          for (const slot of stage.produces || []) requireCondition(output.outputSlots[slot]?.length > 0, 'OUTPUT_MISSING', `Handler did not produce ${slot}`);
          for (const ref of outputRefs) await this.app.artifacts.verify(ref);
          const receipt = { id: id('completion'), caseId, stageId: stage.id, outputSlots: output.outputSlots, handler: actor };
          store.put('completions', receipt.id, receipt, 0);
          await kernel.command(caseId, 'produced', { stageId: stage.id, outputRefs, outputSlots: output.outputSlots, completionReceiptRef: receipt.id });
        } catch (error) {
          const failure = { id: id('failure'), message: error.message, code: error.code || 'HANDLER_FAILED' };
          store.put('failures', failure.id, failure, 0);
          const latest = store.getCase(caseId).stages.find(item => item.id === stage.id);
          if (['running', 'awaitingReview'].includes(latest.status)) {
            await kernel.command(caseId, 'fail', { stageId: stage.id, failureRef: failure.id, unknown: error.sideEffectState === 'unknown' });
          }
          throw error;
        }
      }
      state = store.getCase(caseId); stage = state.stages.find(item => item.id === node.id);
      if (stage.status === 'awaitingReview') {
        const reviewer = this.reviewers[stage.id] || this.reviewers[stage.role];
        requireCondition(reviewer, 'REVIEW_REQUIRED', 'Independent reviewer handler required');
        const review = await reviewer({ state, stage, app: this.app, signal });
        await kernel.command(caseId, 'review', { stageId: stage.id, receiptRef: review.id });
        if (review.verdict !== 'PASS') return store.getCase(caseId);
      }
      if (await this.afterStage?.(store.getCase(caseId).stages.find(item => item.id === node.id))) return store.getCase(caseId);
    }
    return store.getCase(caseId);
  }
}
