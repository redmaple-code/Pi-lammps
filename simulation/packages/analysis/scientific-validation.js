import { digest, requireCondition, utc } from '../contracts/index.js';
import { evaluateMetric } from './index.js';

export function freezeProtocol(protocol) {
  requireCondition(protocol?.mode === 'exploratory' && Array.isArray(protocol.comparisons), 'SCHEMA_INVALID', 'Exploratory protocol and comparisons are required');
  const ids = new Set();
  for (const comparison of protocol.comparisons) {
    requireCondition(comparison.id && !ids.has(comparison.id), 'SCHEMA_INVALID', 'Comparison IDs must be unique');
    ids.add(comparison.id);
    requireCondition(['numerical', 'model', 'task'].includes(comparison.dimension) && comparison.field && comparison.unit,
      'SCHEMA_INVALID', 'Comparison dimension, field and unit are required');
    requireCondition(comparison.methodRef && comparison.reference && comparison.selection && comparison.sampling,
      'SCHEMA_INVALID', 'Comparison needs method, reference, selection and sampling');
    if (comparison.referenceValue !== undefined) {
      requireCondition(Number.isFinite(comparison.referenceValue) && Number.isFinite(comparison.absoluteTolerance)
        && comparison.absoluteTolerance >= 0 && comparison.referenceRefs?.length && comparison.conditions,
      'SCHEMA_INVALID', 'Quantitative reference needs evidence, matching conditions and a finite tolerance');
    }
    if (comparison.borderlineTolerance !== undefined) requireCondition(Number.isFinite(comparison.borderlineTolerance)
      && comparison.borderlineTolerance >= 0, 'SCHEMA_INVALID', 'Borderline tolerance must be finite and nonnegative');
  }
  const content = structuredClone(protocol);
  delete content.digest;
  return { ...content, digest: digest(content) };
}

export function scientificValidation(protocol, measured, { dataRefs, runRef, parameters, measurementUnits }) {
  requireCondition(freezeProtocol(protocol).digest === protocol.digest, 'ARTIFACT_CHANGED', 'Frozen validation protocol changed');
  const comparisons = protocol.comparisons.map(definition => {
    const base = { definitionRef: definition.id, dimension: definition.dimension, unit: definition.unit,
      referenceRefs: definition.referenceRefs || [], dataRefs, runRef, protocolDigest: protocol.digest };
    if (measurementUnits && measurementUnits[definition.field] !== definition.unit) return { ...base, verdict: 'cannotCompute',
      limitations: ['Measurement unit differs from the frozen comparison unit'] };
    if (definition.referenceValue === undefined) return { ...base, verdict: 'cannotCompute',
      limitations: [definition.missingReason || 'Applicable reference data has not been supplied'] };
    const conditionsMatch = Object.entries(definition.conditions).every(([key, value]) => digest(parameters[key] ?? null) === digest(value));
    if (!conditionsMatch || !Number.isFinite(measured[definition.field])) return { ...base, verdict: 'cannotCompute',
      limitations: [conditionsMatch ? 'Required finite measurement is unavailable' : 'Reference conditions differ from the frozen simulation conditions'] };
    const difference = Math.abs(measured[definition.field] - definition.referenceValue);
    const metric = evaluateMetric({ id: definition.id, field: definition.field, quantity: definition.field,
      unit: definition.unit, methodRef: definition.methodRef, reference: definition.reference,
      selection: definition.selection, sampling: definition.sampling,
      acceptanceSpec: { maximum: definition.absoluteTolerance, borderlineTolerance: definition.borderlineTolerance || 0 } }, difference, dataRefs, runRef);
    return { ...base, verdict: metric.verdict, value: measured[definition.field], difference,
      referenceValue: definition.referenceValue, tolerance: definition.absoluteTolerance, limitations: [] };
  });
  const outcome = dimension => {
    const selected = comparisons.filter(item => item.dimension === dimension);
    if (selected.some(item => item.verdict === 'notMet')) return 'failed';
    if (!selected.length || selected.some(item => ['cannotCompute', 'borderline'].includes(item.verdict))) return 'insufficient';
    return 'passed';
  };
  return { protocolDigest: protocol.digest, numerical: outcome('numerical'), model: outcome('model'),
    task: outcome('task'), comparisons, evaluatedAt: utc() };
}
