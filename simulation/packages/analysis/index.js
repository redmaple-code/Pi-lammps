import { id, utc, requireCondition } from '../contracts/index.js';
import { validateRecord } from '../contracts/records.js';

export function outputSupport(definitions, outputs) {
  const available = new Set(outputs);
  return definitions.map(definition => ({ criterionId: definition.id, supported: definition.requiredOutputs.every(output => available.has(output)),
    missingOutputs: definition.requiredOutputs.filter(output => !available.has(output)) }));
}
export function evaluateMetric(definition, value, dataRefs, runRef) {
  validateRecord('MetricDefinition', definition);
  const base = { id: id('metric'), definitionRef: definition.id, quantity: definition.quantity, unit: definition.unit,
    methodRef: definition.methodRef, reference: definition.reference, selection: definition.selection, sampling: definition.sampling,
    dataRefs, runRef, createdAt: utc() };
  if (value === undefined || (typeof value === 'number' && !Number.isFinite(value))) return { ...base, verdict: 'cannotCompute', limitations: ['Required finite data unavailable'] };
  const rule = definition.acceptanceSpec;
  let passed;
  if (rule.equals !== undefined) passed = value === rule.equals;
  else passed = (rule.minimum === undefined || value >= rule.minimum) && (rule.maximum === undefined || value <= rule.maximum);
  let verdict = passed ? 'met' : 'notMet';
  if (rule.borderlineTolerance && typeof value === 'number' &&
    [rule.minimum, rule.maximum].some(bound => bound !== undefined && Math.abs(value - bound) <= rule.borderlineTolerance)) verdict = 'borderline';
  return { ...base, value, verdict, limitations: [] };
}
export function acceptanceSummary(results, { numerical, model }) {
  requireCondition(results.length > 0, 'EVIDENCE_INSUFFICIENT', 'Acceptance criteria missing');
  return { task: results.every(item => item.verdict === 'met') ? 'met' : results.some(item => item.verdict === 'cannotCompute') ? 'cannotCompute' : 'notMet',
    numerical, model, criteria: results };
}
export function logSections(text, { head = 120, tail = 120 } = {}) {
  const lines = text.split(/\r?\n/), selected = new Set();
  for (let i = 0; i < Math.min(head, lines.length); i++) selected.add(i);
  for (let i = Math.max(0, lines.length - tail); i < lines.length; i++) selected.add(i);
  for (let i = 0; i < lines.length; i++) if (/ERROR|WARNING|Lost atoms|Non-numeric|Step\s+/.test(lines[i])) {
    for (let j = Math.max(0, i - 2); j <= Math.min(lines.length - 1, i + 2); j++) selected.add(j);
  }
  return { totalLines: lines.length, sampled: selected.size < lines.length,
    lines: [...selected].sort((a, b) => a - b).map(index => ({ line: index + 1, text: lines[index] })) };
}
