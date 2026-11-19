import { createHash, randomUUID } from 'node:crypto';

export const CONTRACT_VERSION = '1.0.0';
export const id = prefix => `${prefix}-${randomUUID()}`;
export const utc = () => new Date().toISOString();
export class SimulationError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'SimulationError';
    this.code = code;
    Object.assign(this, { retryable: false, sideEffectState: 'none' }, details);
  }
  toJSON() {
    return { code: this.code, message: this.message, retryable: this.retryable,
      sideEffectState: this.sideEffectState, context: this.context, suggestedAction: this.suggestedAction };
  }
}
export function requireCondition(condition, code, message, details) {
  if (!condition) throw new SimulationError(code, message, details);
}
export function canonical(value) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    requireCondition(Number.isFinite(value), 'SCHEMA_INVALID', 'Non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  requireCondition(value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype,
    'SCHEMA_INVALID', 'Expected a JSON value');
  return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
}
export function digestBytes(bytes) { return `sha256:${createHash('sha256').update(bytes).digest('hex')}`; }
export function digest(value) { return digestBytes(canonical(value)); }
export function header(kind, producer, context = {}) {
  requireCondition(producer?.actorId && producer.actorInstanceId && ['human', 'agent', 'service'].includes(producer.kind),
    'IDENTITY_REQUIRED', 'A host-authenticated producer is required');
  return { schemaVersion: CONTRACT_VERSION, id: id(kind), createdAt: utc(), producer: structuredClone(producer), context };
}
export const serviceActor = (name, instanceId = id('instance')) => ({ actorId: name, actorInstanceId: instanceId, kind: 'service' });
export function migrateLegacy(value) {
  const aliases = { not_met: 'notMet', cannot_compute: 'cannotCompute', script_only: 'scriptOnly' };
  const result = structuredClone(value);
  for (const key of ['verdict', 'status']) if (aliases[result[key]]) {
    result.legacy = { ...(result.legacy || {}), [key]: result[key] };
    result[key] = aliases[result[key]];
  }
  return result;
}
export function topological(nodes, dependencyField = 'needs') {
  const index = new Map(nodes.map(node => [node.id, node]));
  requireCondition(index.size === nodes.length, 'SCHEMA_INVALID', 'Duplicate node identity');
  const active = new Set(), done = new Set(), result = [];
  function visit(key) {
    requireCondition(index.has(key), 'REFERENCE_MISSING', `Missing node ${key}`);
    requireCondition(!active.has(key), 'SCHEMA_INVALID', `Dependency cycle at ${key}`);
    if (done.has(key)) return;
    active.add(key);
    for (const dependency of index.get(key)[dependencyField] || []) visit(dependency);
    active.delete(key); done.add(key); result.push(index.get(key));
  }
  for (const key of index.keys()) visit(key);
  return result;
}
export function evaluateCondition(expression, facts) {
  if (expression === undefined) return true;
  if (expression.all) {
    const values = expression.all.map(item => evaluateCondition(item, facts));
    return values.includes(false) ? false : values.includes('unknown') ? 'unknown' : true;
  }
  if (expression.any) {
    const values = expression.any.map(item => evaluateCondition(item, facts));
    return values.includes(true) ? true : values.includes('unknown') ? 'unknown' : false;
  }
  if (expression.not) {
    const value = evaluateCondition(expression.not, facts);
    return value === 'unknown' ? value : !value;
  }
  const actual = expression.fact?.split('.').reduce((object, key) => object?.[key], facts);
  if (actual === undefined) return 'unknown';
  const expected = expression.value;
  if (expression.op === 'in') return Array.isArray(expected) ? expected.includes(actual) : 'unknown';
  if (typeof actual !== typeof expected) return 'unknown';
  switch (expression.op) {
    case 'eq': return canonical(actual) === canonical(expected);
    case 'ne': return canonical(actual) !== canonical(expected);
    case 'gt': return typeof actual === 'number' ? actual > expected : 'unknown';
    case 'gte': return typeof actual === 'number' ? actual >= expected : 'unknown';
    case 'lt': return typeof actual === 'number' ? actual < expected : 'unknown';
    case 'lte': return typeof actual === 'number' ? actual <= expected : 'unknown';
    default: throw new SimulationError('SCHEMA_INVALID', `Unknown condition operator ${expression.op}`);
  }
}
