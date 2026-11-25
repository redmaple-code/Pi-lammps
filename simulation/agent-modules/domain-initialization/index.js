import Ajv from 'ajv/dist/2020.js';
import { defineModule, requireThat } from '../core/index.js';
export { DomainInitializationService, questionCatalog, semanticDigest } from './service.js';
export { FileLibraryRepository, MemoryLibraryRepository } from './repository.js';

const text = { type: 'string', minLength: 1, maxLength: 8000 };
const short = { type: 'string', minLength: 1, maxLength: 200 };
const libraryId = { type: 'string', pattern: '^library-[a-f0-9-]{36}$' };
const identity = { libraryId };
const write = { ...identity, operationId: { type: 'string', pattern: '^[A-Za-z0-9._-]{1,128}$' },
  expectedRevision: { type: 'integer', minimum: 1 } };
const strings = { type: 'array', items: short, maxItems: 32 };
const schema = (properties, required) => ({ type: 'object', properties, required, additionalProperties: false });
export const operationSchemas = {
  create: schema({ title: short, objective: text, domainId: { type: 'string', pattern: '^[a-z][a-z0-9-]{0,127}$' },
    operationId: write.operationId }, ['title']),
  list: schema({}, []), status: schema(identity, ['libraryId']), guide: schema(identity, ['libraryId']),
  answer: schema({ ...write, questionKey: short, answer: text, status: { enum: ['answered', 'unknown', 'deferred'] } }, ['libraryId', 'questionKey', 'answer']),
  source: schema({ ...write, title: short, content: { type: 'string', minLength: 1, maxLength: 262144 },
    sourceType: { enum: ['caseReport', 'standard', 'manual', 'paper', 'userAccount', 'log', 'dataset', 'other'] },
    locator: text, originGroupId: short, sourceMetadata: { type: 'object' } }, ['libraryId', 'title', 'content', 'sourceType']),
  readSource: schema({ ...identity, sourceId: short, startLine: { type: 'integer', minimum: 1 },
    endLine: { type: 'integer', minimum: 1 }, page: { type: 'integer', minimum: 0 } }, ['libraryId', 'sourceId']),
  propose: schema({ ...write, kind: { enum: ['concept', 'claim', 'method', 'practice', 'observation', 'resource'] },
    statement: text, conditions: strings, exceptions: strings, sourceIds: { ...strings, minItems: 1 },
    evidenceRanges: { type: 'array', maxItems: 32, items: schema({ sourceId: short,
      startLine: { type: 'integer', minimum: 1 }, endLine: { type: 'integer', minimum: 1 } }, ['sourceId', 'startLine', 'endLine']) },
    tags: { ...strings, uniqueItems: true }, replaceProposalId: short }, ['libraryId', 'statement', 'sourceIds']),
  decide: schema({ ...write, proposalId: short, subjectDigest: { type: 'string', pattern: '^sha256:[a-f0-9]{64}$' },
    verdict: { enum: ['adopt', 'defer', 'reject'] }, intendedUse: { enum: ['referenceOnly', 'projectPractice', 'packCandidate', 'activeInScope'] },
    reason: text }, ['libraryId', 'proposalId', 'subjectDigest', 'verdict']),
  query: schema({ ...identity, query: { type: 'string', maxLength: 4096 }, includeCandidates: { type: 'boolean' }, tags: strings }, ['libraryId']),
  research: schema(identity, ['libraryId']), snapshot: schema(identity, ['libraryId']),
  pause: schema({ ...identity, paused: { type: 'boolean' }, expectedRevision: { type: 'integer', minimum: 1 } }, ['libraryId', 'paused'])
};
operationSchemas.decideMany = schema({ ...write, decisions: { type: 'array', minItems: 1, maxItems: 50,
  items: schema(Object.fromEntries(['proposalId', 'subjectDigest', 'verdict', 'intendedUse', 'reason']
    .map(name => [name, operationSchemas.decide.properties[name]])), ['proposalId', 'subjectDigest', 'verdict']) } }, ['libraryId', 'decisions']);
const reads = new Set(['list', 'status', 'guide', 'readSource', 'query', 'research', 'snapshot']);
const humanOperations = new Set(['answer', 'decide', 'decideMany', 'pause']);

export function createDomainInitializationModule({ service }) {
  const ajv = new Ajv({ allErrors: true });
  const operations = {};
  for (const [name, inputSchema] of Object.entries(operationSchemas)) {
    const validate = ajv.compile(inputSchema);
    operations[name] = { effect: reads.has(name) ? 'read' : 'write', capability: `domain.initialize.${name}`,
      async invoke(input, actor) {
        requireThat(validate(input), 'SCHEMA_INVALID', 'Invalid domain initialization request');
        if (humanOperations.has(name)) requireThat(actor.kind === 'human', 'USER_DECISION_REQUIRED', 'This operation needs a direct user interaction');
        return name === 'guide' ? service.status(input) : service[name](input, actor);
      } };
  }
  return defineModule({ id: 'domain-initialization', operations });
}
