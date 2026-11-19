import { digest, requireCondition } from '../contracts/index.js';

export function expandStudy({ studyId, parameters, seeds, maxRuns, budget }) {
  let samples = [{}];
  for (const [key, values] of Object.entries(parameters)) {
    requireCondition(values.length > 0, 'SCHEMA_INVALID', 'Empty study parameter');
    samples = samples.flatMap(sample => values.map(value => ({ ...sample, [key]: value })));
  }
  if (seeds) samples = samples.flatMap(sample => seeds.map(seed => ({ ...sample, seed })));
  requireCondition(samples.length <= maxRuns && budget > 0, 'RESOURCE_LIMIT', 'Study exceeds run limit');
  return samples.map((sample, index) => ({ studyId, sample, sampleIndex: index,
    runKey: digest({ studyId, sample }), budget: budget / samples.length }));
}
export async function runIterations({ maxIterations, budget, initial, step, converged, checkpoint, signal }) {
  requireCondition(Number.isInteger(maxIterations) && maxIterations > 0, 'SCHEMA_INVALID', 'Bounded iterations required');
  let value = initial, cost = 0;
  const history = [];
  for (let iterationIndex = 0; iterationIndex < maxIterations; iterationIndex++) {
    if (signal?.aborted) return { status: 'cancelled', value, cost, history };
    if (cost >= budget) return { status: 'budgetExhausted', value, cost, history };
    const result = await step({ value, iterationIndex, remainingBudget: budget - cost, signal });
    requireCondition(result.cost >= 0 && Number.isFinite(result.cost) && cost + result.cost <= budget, 'RESOURCE_LIMIT', 'Iteration budget exceeded');
    value = result.value; cost += result.cost;
    history.push({ iterationIndex, value, cost: result.cost });
    await checkpoint?.({ history, value, cost });
    if (await converged(value, history)) return { status: 'converged', value, cost, history };
  }
  return { status: 'limitReached', value, cost, history };
}
export function validateExchange(record, contract) {
  for (const key of ['quantity', 'unit', 'coordinateFrame', 'entityAssociation', 'timeReference']) {
    requireCondition(record[key] === contract[key], 'SEMANTIC_MISMATCH', `Exchange ${key} differs`);
  }
  requireCondition(record.sequence >= 0 && record.values.every(Number.isFinite), 'SCHEMA_INVALID', 'Invalid exchange values');
  requireCondition(record.mappingError <= contract.maxMappingError, 'SCIENTIFIC_CHECK_FAILED', 'Exchange mapping error exceeds bound');
  return { status: 'passed', digest: digest(record) };
}
export function validateCheckpointBundle(bundle) {
  requireCondition(bundle.checkpoints.length >= 2, 'SCHEMA_INVALID', 'Coupled checkpoint requires all submodels');
  requireCondition(bundle.checkpoints.every(item => item.time === bundle.time && item.exchangeSequence === bundle.exchangeSequence
    && item.digest && item.lockRef), 'CHECKPOINT_INCONSISTENT', 'Submodels do not share a consistent checkpoint');
  return { status: 'passed', digest: digest(bundle) };
}
