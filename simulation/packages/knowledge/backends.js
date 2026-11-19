import { requireCondition, utc } from '../contracts/index.js';

export class KnowledgeBackendRegistry {
  constructor() { this.backends = new Map(); }
  register(name, backend) {
    requireCondition(name && !this.backends.has(name), 'SCHEMA_INVALID', 'Backend identity is already registered');
    for (const method of ['search', 'read', 'validate', 'propose']) requireCondition(typeof backend[method] === 'function', 'CAPABILITY_UNAVAILABLE', `Knowledge backend needs ${method}`);
    this.backends.set(name, backend);
  }
  get(name) {
    requireCondition(this.backends.has(name), 'CAPABILITY_UNAVAILABLE', `Knowledge backend ${name} is unavailable`);
    return this.backends.get(name);
  }
}
export class UKBKnowledgeAdapter {
  constructor(client, { frozenRevisions = false } = {}) { this.client = client; this.frozenRevisions = frozenRevisions; }
  async search(query, scope) {
    const result = await this.client.search(query, scope);
    return { ...result, items: result.items.map(item => ({ ...item,
      provenanceStatus: this.frozenRevisions && item.revisionId && item.digest ? 'frozen' : 'unfrozen', backend: 'ukb' })) };
  }
  async read(ref) {
    const result = await this.client.read(ref);
    return { ...result, sourceRef: ref, accessedAt: utc(), representation: result.representation || 'section',
      provenanceStatus: this.frozenRevisions && result.digest ? 'frozen' : 'unfrozen' };
  }
  async validate(ref) {
    if (!this.frozenRevisions) return { status: 'unfrozen', limitations: ['Backend does not provide immutable source revisions'] };
    return this.client.validate(ref);
  }
  async propose(change) { return { ...(await this.client.propose(change)), status: 'candidate' }; }
}
