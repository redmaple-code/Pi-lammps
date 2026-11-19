import { defineModule, requireThat } from '../core/index.js';

export function createEvidenceModule({ backend, resolveSource }) {
  requireThat(typeof backend?.read === 'function' && typeof backend?.assess === 'function' && typeof resolveSource === 'function',
    'CONFIG_INVALID', 'Evidence backend and trusted source resolver required');
  return defineModule({ id: 'evidence', operations: {
    read: { effect: 'write', capability: 'evidence.read', async invoke(input, actor) {
      requireThat(typeof input.sourceId === 'string' && input.sourceId.length > 0, 'SCHEMA_INVALID', 'sourceId required');
      const source = await resolveSource(input.sourceId, actor);
      requireThat(source?.sourceId === input.sourceId, 'REFERENCE_MISSING', 'Trusted evidence source not found');
      return backend.read(source, input.scope || {}, actor);
    } },
    assess: { effect: 'read', capability: 'evidence.assess', async invoke(input, actor) {
      requireThat(Array.isArray(input.readRefs) && input.readRefs.length > 0, 'EVIDENCE_INSUFFICIENT', 'readRefs required');
      return backend.assess(input.readRefs, input.policy || {}, actor.actorInstanceId);
    } }
  } });
}
