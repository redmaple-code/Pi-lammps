import { createHash, randomUUID } from 'node:crypto';

export const PROTOCOL = '1.0.0';
export class ModuleError extends Error {
  constructor(code, message, sideEffectState = 'none') {
    super(message); this.name = 'ModuleError'; this.code = code;
    this.retryable = false; this.sideEffectState = sideEffectState;
    this[Symbol.for('redmaple.agent-modules.error.v1')] = true;
  }
}
export function requireThat(value, code, message) {
  if (!value) throw new ModuleError(code, message);
}
export function digestJson(value) {
  function encode(x) {
    if (x === null || typeof x === 'string' || typeof x === 'boolean') return JSON.stringify(x);
    if (typeof x === 'number' && Number.isFinite(x)) return JSON.stringify(x);
    if (Array.isArray(x)) return `[${x.map(encode).join(',')}]`;
    requireThat(x && typeof x === 'object' && Object.getPrototypeOf(x) === Object.prototype, 'SCHEMA_INVALID', 'Expected JSON input');
    return `{${Object.keys(x).sort().map(k => `${JSON.stringify(k)}:${encode(x[k])}`).join(',')}}`;
  }
  return `sha256:${createHash('sha256').update(encode(value)).digest('hex')}`;
}
export function defineModule({ id, version = '1.0.0', operations }) {
  requireThat(/^[a-z][a-z0-9-]*$/.test(id) && operations && Object.keys(operations).length, 'SCHEMA_INVALID', 'Invalid module manifest');
  for (const [name, op] of Object.entries(operations)) {
    requireThat(/^[a-z][a-zA-Z0-9]*$/.test(name) && typeof op.invoke === 'function' && typeof op.capability === 'string'
      && ['read', 'write'].includes(op.effect), 'SCHEMA_INVALID', 'Invalid operation descriptor');
  }
  return Object.freeze({ id, version, protocol: PROTOCOL, operations: Object.freeze({ ...operations }) });
}

// The authenticated actor is supplied by the embedding host, never by operation input.
export class ModuleHost {
  constructor({ audit, authorize }) {
    requireThat(typeof audit?.append === 'function' && typeof authorize === 'function', 'CONFIG_INVALID', 'Durable audit and host authorization are required');
    this.audit = audit; this.authorize = authorize; this.modules = new Map();
  }
  register(module) {
    requireThat(module?.protocol === PROTOCOL && !this.modules.has(module.id), 'VERSION_INCOMPATIBLE', 'Incompatible or duplicate module');
    this.modules.set(module.id, module);
  }
  unregister(id) { return this.modules.delete(id); }
  list() {
    return [...this.modules.values()].map(m => ({ id: m.id, version: m.version, protocol: m.protocol,
      operations: Object.entries(m.operations).map(([name, o]) => ({ name, effect: o.effect, capability: o.capability })) }));
  }
  async call(moduleId, operation, input, context) {
    requireThat(context?.actor?.actorId && context.actor.actorInstanceId && ['human','agent','service'].includes(context.actor.kind),
      'IDENTITY_REQUIRED', 'Authenticated host actor required');
    requireThat(typeof context.requestId === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(context.requestId), 'SCHEMA_INVALID', 'Valid requestId required');
    requireThat(input && typeof input === 'object' && !Array.isArray(input), 'SCHEMA_INVALID', 'Input object required');
    const module = this.modules.get(moduleId), op = module?.operations[operation];
    requireThat(op, 'CAPABILITY_UNAVAILABLE', 'Module or operation unavailable');
    const callId = randomUUID();
    // The audit never stores payload, query, path, actor name, or secrets.
    const base = { callId, module: moduleId, version: module.version, operation, effect: op.effect,
      requestIdDigest: digestJson(context.requestId), actorInstanceDigest: digestJson(context.actor.actorInstanceId),
      actorKind: context.actor.kind, inputDigest: digestJson(input) };
    try { await this.audit.append({ ...base, phase: 'started' }); }
    catch { throw new ModuleError('AUDIT_UNAVAILABLE', 'Audit unavailable before operation'); }
    let result;
    try {
      requireThat(await this.authorize(context.actor, op.capability, { moduleId, operation, input: structuredClone(input) }) === true,
        'PERMISSION_DENIED', 'Host denied capability');
      result = await op.invoke(structuredClone(input), context.actor);
    } catch (error) {
      const code = typeof error?.code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(error.code) ? error.code : 'MODULE_FAILURE';
      const knownError = error?.[Symbol.for('redmaple.agent-modules.error.v1')] === true;
      const state = knownError && ['none','unknown'].includes(error.sideEffectState)
        ? error.sideEffectState : (op.effect === 'write' ? 'unknown' : 'none');
      try { await this.audit.append({ ...base, phase: 'failed', code, sideEffectState: state }); }
      catch { throw new ModuleError('AUDIT_UNAVAILABLE', 'Audit failed after operation', op.effect === 'write' ? 'unknown' : 'none'); }
      throw new ModuleError(code, 'Module operation failed; inspect host records', state);
    }
    try { await this.audit.append({ ...base, phase: 'completed', outputDigest: digestJson(result) }); }
    catch { throw new ModuleError('AUDIT_UNAVAILABLE', 'Audit failed after operation; reconcile before retry', op.effect === 'write' ? 'unknown' : 'none'); }
    return result;
  }
}
