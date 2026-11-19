import { id, utc, requireCondition } from '../contracts/index.js';
export function diagnoseFailure({ runRef, log, readScope }) {
  const signals = [];
  if (/not found|not recognized|ENOENT/i.test(log)) signals.push({ category: 'environment', actor: 'executor', earliestStage: 'execution' });
  if (/Unknown command|Illegal|Cannot open|Incorrect args/i.test(log)) signals.push({ category: 'input', actor: 'writer', earliestStage: 'input' });
  if (/Lost atoms|Bond atoms missing|Non-numeric/i.test(log)) signals.push({ category: 'numerical', actor: 'analyst', earliestStage: 'undetermined' });
  return { id: id('diagnosis'), runRef, readScope, signals, status: signals.length ? 'hypothesesRequired' : 'insufficientEvidence',
    limitations: ['Signals identify investigation routes. Root causes require input, structure, potential and pre-failure data.'], createdAt: utc() };
}
export function planRepair(diagnosis, { hypothesis, evidenceRefs, changes, earliestStage, requiredReviews, chainId }) {
  requireCondition(hypothesis && evidenceRefs?.length && changes?.length && earliestStage && chainId, 'EVIDENCE_INSUFFICIENT', 'Bounded repair needs a hypothesis and evidence');
  return { id: id('repair-plan'), diagnosisRef: diagnosis.id, chainId, hypothesis, evidenceRefs, changes, earliestStage, requiredReviews,
    status: 'proposed', autoApplied: false, createdAt: utc() };
}
