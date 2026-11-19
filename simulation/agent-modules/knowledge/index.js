import { defineModule, requireThat } from '../core/index.js';

export function createKnowledgeModule({ backend }) {
  requireThat(typeof backend?.search === 'function' && typeof backend?.read === 'function', 'CONFIG_INVALID', 'Knowledge backend needs search and read');
  return defineModule({ id: 'knowledge', operations: {
    search: { effect: 'read', capability: 'knowledge.search', async invoke(input) {
      requireThat(typeof input.query === 'string' && input.query.trim().length > 0 && input.query.length <= 4096, 'SCHEMA_INVALID', 'query required');
      const options = input.options || {};
      requireThat(Object.keys(options).every(k => ['domain','limit'].includes(k))
        && (options.domain === undefined || typeof options.domain === 'string')
        && (options.limit === undefined || (Number.isInteger(options.limit) && options.limit >= 1 && options.limit <= 50)),
      'SCHEMA_INVALID', 'Invalid search options');
      return backend.search(input.query, options);
    } },
    read: { effect: 'write', capability: 'knowledge.read', async invoke(input, actor) {
      requireThat(typeof input.sourceId === 'string' && input.sourceId.length > 0, 'SCHEMA_INVALID', 'sourceId required');
      return backend.read(input.sourceId, input.scope || {}, actor);
    } }
  } });
}
