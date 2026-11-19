import { defineModule, requireThat } from '../core/index.js';

export function createGateModule({ backend }) {
  requireThat(typeof backend?.submit === 'function', 'CONFIG_INVALID', 'Gate backend needs submit');
  return defineModule({ id: 'gate', operations: {
    submit: { effect: 'write', capability: 'review.submit', async invoke(input, actor) {
      requireThat(typeof input.caseId === 'string' && typeof input.stageId === 'string' && Array.isArray(input.checks)
        && input.checks.length > 0 && Array.isArray(input.subjectRefs) && input.subjectRefs.length > 0,
        'SCHEMA_INVALID', 'case, stage, subjects and checks required');
      const ids = input.checks.map(c => c.checkId);
      requireThat(ids.every(id => typeof id === 'string' && id.length > 0) && new Set(ids).size === ids.length,
        'SCHEMA_INVALID', 'Check IDs must be unique');
      return backend.submit(input, actor);
    } }
  } });
}
