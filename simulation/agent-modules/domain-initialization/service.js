import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import Ajv from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { digestJson, requireThat } from '../core/index.js';
import { checkLibraryContent, checkMaterialPath } from './privacy.js';
import { createHash } from 'node:crypto';

export const questionCatalog = JSON.parse(readFileSync(new URL('./assets/question-catalog.json', import.meta.url), 'utf8'));
const ajv = new Ajv({ allErrors: true, strict: false });
addFormats(ajv);
const validateBundle = ajv.compile(JSON.parse(readFileSync(new URL('./assets/authoring-bundle.schema.json', import.meta.url), 'utf8')));
const now = () => new Date().toISOString();
const ref = record => ({ id: record.id, revision: record.revision });
const recordKey = value => `${value.id}@${value.revision}`;
const human = actor => requireThat(actor?.kind === 'human', 'USER_DECISION_REQUIRED', 'A direct user interaction is required');
const ignored = {
  knowledge: ['lifecycle', 'useDecision', 'decisionRefs'], tag: ['status', 'decisionRefs'],
  annotation: ['status', 'decisionRefs'], binding: ['status', 'decisionRefs']
};

export function semanticDigest(record) {
  const data = structuredClone(record.data);
  for (const field of ignored[record.recordType] || []) delete data[field];
  return digestJson({ recordType: record.recordType, data });
}

function addRecord(document, recordType, data, actor, base) {
  const previousRevision = base ? Math.max(...document.bundle.records.filter(item => item.id === base.id).map(item => item.revision)) : 0;
  const record = { id: base?.id || `${recordType}-${randomUUID()}`, revision: previousRevision + 1,
    createdAt: now(), actor: { id: actor.actorId, kind: actor.kind === 'human' ? 'user' : actor.kind === 'agent' ? 'ai' : 'system' },
    originMode: actor.kind === 'human' ? 'userProvided' : actor.kind === 'agent' ? 'aiProposed' : 'systemDerived',
    recordType, data: structuredClone(data) };
  if (base) record.adoptionBaseRef = ref(base);
  document.bundle.records.push(record);
  return record;
}

function resolveRecord(document, reference, type) {
  const record = document.bundle.records.find(item => recordKey(item) === recordKey(reference));
  requireThat(record && (!type || record.recordType === type), 'REFERENCE_MISSING', 'Referenced library record is missing');
  return record;
}

function currentRecords(document, type) {
  const records = new Map();
  for (const record of document.bundle.records) if (!type || record.recordType === type) records.set(record.id, record);
  return [...records.values()];
}

function resolveAnsweredGaps(document, catalogId, sourceId, actor) {
  for (const gap of currentRecords(document, 'gap')) {
    const sameScope = sourceId ? gap.data.knownRefs.some(reference => reference.id === sourceId) : !gap.data.knownRefs.length;
    if (gap.data.affectedDecision === catalogId && sameScope && gap.data.status !== 'resolved') {
      addRecord(document, 'gap', { ...gap.data, status: 'resolved', nextAction: '按已补充的回答或资料继续制作。', recoveryCondition: '相关输入已补充。' }, actor, gap);
    }
  }
}

function validateDocument(document) {
  requireThat(validateBundle(document.bundle), 'SCHEMA_INVALID', 'Domain-library bundle failed structural validation');
  const records = new Map(document.bundle.records.map(record => [recordKey(record), record]));
  requireThat(records.size === document.bundle.records.length, 'SCHEMA_INVALID', 'Duplicate record revision');
  for (const record of document.bundle.records) {
    requireThat(record.originMode !== 'demonstration', 'SCHEMA_INVALID', 'Demonstration content needs a separate candidate import');
    if (record.recordType === 'source') requireThat(record.data.digest === `sha256:${createHash('sha256').update(record.data.inlineText || '').digest('hex')}`,
      'SOURCE_CHANGED', 'Registered source content changed');
    if (record.recordType === 'fragment') {
      const source = resolveRecord(document, record.data.sourceRef, 'source');
      requireThat(source.data.inlineText?.includes(record.data.quote), 'SOURCE_CHANGED', 'Source quote no longer matches');
    }
    if (record.recordType === 'knowledge') {
      for (const source of record.data.sourceRefs) resolveRecord(document, source, 'fragment');
    }
    if (record.recordType === 'annotation') {
      resolveRecord(document, record.data.targetRef);
      for (const source of record.data.anchorRefs) resolveRecord(document, source, 'fragment');
      for (const assignment of record.data.assignments) {
        const tag = resolveRecord(document, assignment.tagRef, 'tag');
        if (record.data.status === 'accepted') requireThat(tag.data.status === 'accepted', 'USER_DECISION_REQUIRED', 'Annotation needs an adopted tag');
      }
    }
    const adopted = record.recordType === 'knowledge' ? record.data.lifecycle === 'confirmed'
      : ['tag', 'annotation', 'binding'].includes(record.recordType) && record.data.status === 'accepted';
    if (!adopted) continue;
    requireThat(record.adoptionBaseRef, 'USER_DECISION_REQUIRED', 'Adopted content requires its reviewed revision');
    const base = resolveRecord(document, record.adoptionBaseRef, record.recordType);
    requireThat(base.id === record.id && base.revision < record.revision && semanticDigest(base) === semanticDigest(record),
      'DECISION_SCOPE_MISMATCH', 'Adopted content differs from the reviewed candidate');
    const covered = record.data.decisionRefs.some(reference => {
      const decision = resolveRecord(document, reference, 'decision');
      return decision.actor.kind === 'user' && decision.data.verdict === 'adopt'
        && decision.data.subjects.some(subject => recordKey(subject.ref) === recordKey(base) && subject.contentDigest === semanticDigest(base))
        && (record.recordType !== 'knowledge' || (decision.data.intendedUse === record.data.useDecision
          && record.data.scope.every(scope => decision.data.scope.includes(scope))));
    });
    requireThat(covered, 'USER_DECISION_REQUIRED', 'Adopted content has no covering user decision');
  }
  checkLibraryContent(document);
}

export class DomainInitializationService {
  constructor({ repository }) {
    requireThat(['read', 'list', 'save'].every(name => typeof repository?.[name] === 'function'),
      'CONFIG_INVALID', 'Domain initialization needs a revisioned repository');
    this.repository = repository;
  }
  capabilities() {
    return { protocol: 'domain-initialization/1', persistence: 'repository-adapter',
      features: ['guided-interview', 'sources', 'candidate-tags', 'scoped-user-adoption', 'query', 'research-brief', 'snapshot'],
      storageTypeSelection: 'deferred' };
  }
  async load(libraryId) {
    const document = await this.repository.read(libraryId);
    requireThat(document, 'REFERENCE_MISSING', 'Domain library does not exist');
    validateDocument(document);
    return document;
  }
  async list() {
    return { libraries: (await this.repository.list()).map(document => this.summary(document)) };
  }
  summary(document) {
    return { libraryId: document.id, revision: document.revision, title: document.title,
      domainId: document.bundle.session.targetPackId, objective: document.bundle.session.objective,
      status: document.bundle.session.status, sources: currentRecords(document, 'source').length,
      pendingProposals: document.proposals.filter(item => item.status === 'pending').length,
      adoptedKnowledge: currentRecords(document, 'knowledge').filter(item => item.data.lifecycle === 'confirmed').length,
      readiness: document.bundle.session.readiness };
  }
  async create({ title, objective = '待用户说明任务与交付。', domainId, operationId }, actor) {
    requireThat(title?.trim() && objective.trim(), 'SCHEMA_INVALID', 'A meaningful title and objective are required');
    checkLibraryContent({ title, objective, domainId });
    const fingerprint = digestJson({ title, objective, domainId: domainId || null });
    const seed = operationId && digestJson({ operationId, actorId: actor.actorId }).slice(7, 39);
    const id = seed ? `library-${seed.slice(0, 8)}-${seed.slice(8, 12)}-${seed.slice(12, 16)}-${seed.slice(16, 20)}-${seed.slice(20)}` : `library-${randomUUID()}`;
    const previous = await this.repository.read(id);
    if (previous) {
      requireThat(previous.creationDigest === fingerprint, 'IDEMPOTENCY_CONFLICT', 'Creation operation has different input');
      validateDocument(previous);
      return { ...this.summary(previous), nextQuestions: this.questions(previous) };
    }
    const document = { format: 'domain-initialization/1', id, revision: 1, title,
      createdAt: now(), updatedAt: now(), creationDigest: fingerprint, answers: [], proposals: [], operations: [],
      bundle: { schemaVersion: '1.0.0', mode: 'authoring', storageSelection: 'deferred',
        session: { id, revision: 1, objective, targetPackId: domainId || `domain-${id.slice(-12)}`,
          scope: [title], scopeState: 'proposed', status: 'active',
          adoptionPolicy: { semanticChangesRequireUserDecision: true, mechanicalActionsWithinAuthorization: true, batchDecisionsAllowed: true },
          lastQuestionRefs: [], decisionRefs: [], readiness: { design: 'inProgress', knowledge: 'unassessed', integration: 'unassessed', validation: 'unassessed' } },
        records: [], relations: [], externalRefs: [] } };
    if (actor.kind === 'human' && objective !== '待用户说明任务与交付。') {
      document.answers.push({ key: 'global:Q-02', catalogId: 'Q-02', answer: objective, status: 'answered', actor: actor.actorId, at: now() });
    }
    validateDocument(document);
    await this.repository.save(document, 0);
    return { ...this.summary(document), nextQuestions: this.questions(document) };
  }
  async mutate(input, actor, apply) {
    checkLibraryContent(input);
    const document = await this.load(input.libraryId);
    const requestDigest = digestJson(input);
    if (input.operationId) {
      const previous = document.operations.find(item => item.id === input.operationId);
      if (previous) {
        requireThat(previous.requestDigest === requestDigest, 'IDEMPOTENCY_CONFLICT', 'Operation identity already has different input');
        return structuredClone(previous.result);
      }
    }
    requireThat(document.bundle.session.status === 'active', 'SESSION_PAUSED', 'Resume this library before editing it');
    if (input.expectedRevision !== undefined) requireThat(document.revision === input.expectedRevision, 'REVISION_CONFLICT', 'Library changed; reload first');
    const expectedRevision = document.revision;
    const result = await apply(document, actor);
    document.revision++;
    document.bundle.session.revision = document.revision;
    document.updatedAt = now();
    const response = { ...result, libraryId: document.id, revision: document.revision, nextQuestions: this.questions(document) };
    if (input.operationId) document.operations.push({ id: input.operationId, requestDigest, result: response, actorInstanceId: actor.actorInstanceId });
    validateDocument(document);
    await this.repository.save(document, expectedRevision);
    return response;
  }
  questions(document) {
    const result = [];
    const ask = (catalogId, source) => {
      const key = `${source?.id || 'global'}:${catalogId}`;
      if (document.answers.some(answer => answer.key === key)) return;
      const item = questionCatalog.questions.find(question => question.id === catalogId);
      result.push({ key, catalogId, sourceId: source?.id || null, prompt: item.prompt, purpose: item.purpose, affectedFields: item.affectedFields });
    };
    ask('Q-01'); ask('Q-02');
    const sources = currentRecords(document, 'source');
    if (!sources.length) ask('Q-03');
    for (const source of sources) {
      if (source.data.sourceMetadata?.extractionCoverage?.limitations?.length) ask('Q-05', source);
      if (['caseReport', 'userAccount', 'log'].includes(source.data.sourceType)) {
        for (const id of ['Q-06', 'Q-07', 'Q-08', 'Q-09', 'Q-10']) ask(id, source);
      } else if (source.data.sourceType === 'standard') {
        for (const id of ['Q-11', 'Q-12', 'Q-13']) ask(id, source);
      } else ask('Q-04', source);
    }
    if (document.proposals.some(item => item.status === 'pending')) { ask('Q-14'); ask('Q-15'); }
    if (currentRecords(document, 'knowledge').some(item => item.data.lifecycle === 'confirmed')) ask('Q-16');
    ask('Q-17'); ask('Q-18');
    return result.slice(0, 3);
  }
  async status({ libraryId }) {
    const document = await this.load(libraryId);
    return { ...this.summary(document), nextQuestions: this.questions(document), answers: document.answers,
      gaps: currentRecords(document, 'gap').map(record => ({ id: record.id, ...record.data })),
      sources: currentRecords(document, 'source').map(record => ({ id: record.id, title: record.data.title, sourceType: record.data.sourceType,
        digest: record.data.digest, originGroupId: record.data.originGroupId, readCoverage: record.data.readCoverage })),
      proposals: document.proposals.map(proposal => this.proposalView(document, proposal)) };
  }
  async answer(input, actor) {
    human(actor);
    return this.mutate(input, actor, document => {
      const existing = document.answers.find(item => item.key === input.questionKey);
      const question = this.questions(document).find(item => item.key === input.questionKey);
      requireThat(question || existing, 'REFERENCE_MISSING', 'Choose a current question or an earlier answer to correct');
      const catalogId = question?.catalogId || existing.catalogId;
      const answer = { key: input.questionKey, catalogId, answer: input.answer, status: input.status || 'answered',
        actor: actor.actorId, at: now() };
      if (existing) {
        document.answers = document.answers.filter(item => item.key !== input.questionKey);
        document.answerHistory ||= [];
        document.answerHistory.push(existing);
        for (const proposal of document.proposals) if (proposal.status === 'adopted') proposal.needsReassessment = true;
      }
      document.answers.push(answer);
      const sourceId = question?.sourceId || input.questionKey.split(':')[0];
      if (answer.status === 'answered') resolveAnsweredGaps(document, catalogId, sourceId === 'global' ? null : sourceId, actor);
      if (answer.status !== 'answered') addRecord(document, 'gap', {
        question: question?.prompt || '补充先前问题', affectedDecision: catalogId, impact: '相关经验或设计条件待补。',
        status: answer.status === 'deferred' ? 'deferred' : 'open', knownRefs: sourceId === 'global' ? [] : [ref(currentRecords(document, 'source').find(item => item.id === sourceId))],
        nextAction: '根据用户补充或新材料继续核查。', recoveryCondition: '获得相关回答或资料。'
      }, actor);
      if (catalogId === 'Q-02' && answer.status === 'answered') document.bundle.session.objective = input.answer;
      return { answered: answer.key, status: answer.status, corrected: !!existing };
    });
  }
  async source(input, actor) {
    requireThat(input.content.trim() && input.title.trim(), 'SCHEMA_INVALID', 'Material title and content must be meaningful');
    if (input.locator) checkMaterialPath(input.locator);
    return this.mutate(input, actor, document => {
      const hash = `sha256:${createHash('sha256').update(input.content).digest('hex')}`;
      const prior = currentRecords(document, 'source').find(record => record.data.digest === hash);
      if (prior) return { sourceId: prior.id, duplicate: true };
      const source = addRecord(document, 'source', { title: input.title, sourceType: input.sourceType,
        locator: input.locator || `library:${document.id}`, originGroupId: input.originGroupId || `origin-${randomUUID()}`,
        provenance: actor.kind === 'human' ? (input.sourceType === 'userAccount' ? 'userAccount' : 'originalReport') : 'analystReconstruction', identityState: 'frozen', digest: hash,
        readCoverage: { representation: input.sourceType === 'userAccount' ? 'transcript' : 'section',
      scope: '用户注入的文本范围；其它未提供部分待补。', limitations: [] },
        usage: { scope: document.bundle.session.scope, exportMode: 'content', basis: '当前领域库的资料注入。' },
        inlineText: input.content, ...(input.sourceMetadata ? { sourceMetadata: input.sourceMetadata } : {}) }, actor);
      const fragment = addRecord(document, 'fragment', { sourceRef: ref(source), location: { kind: 'section', value: '注入文本' },
        quote: input.content, extraction: input.sourceType === 'userAccount' ? 'transcription' : 'manual', anchorStatus: 'matched', limitations: [] }, actor);
      addRecord(document, 'intake', { sourceRefs: [ref(source)], contributor: actor.actorId,
        receivedVia: input.locator ? 'upload' : 'conversation', purpose: '注入行业案例、规范或经验。', status: 'registered' }, actor);
      resolveAnsweredGaps(document, 'Q-03', null, actor);
      document.bundle.session.readiness.knowledge = 'inProgress';
      return { sourceId: source.id, fragmentRef: ref(fragment), digest: hash, duplicate: false };
    });
  }
  async readSource({ libraryId, sourceId, startLine = 1, endLine, page }) {
    const document = await this.load(libraryId);
    const source = currentRecords(document, 'source').find(record => record.id === sourceId);
    requireThat(source, 'REFERENCE_MISSING', 'Source is not registered in this library');
    const lines = source.data.inlineText.split(/\r?\n/);
    requireThat(!endLine || endLine >= startLine, 'SCHEMA_INVALID', 'Source line range is reversed');
    const lastLine = Math.min(endLine || startLine + 99, lines.length);
    const selected = lines.slice(startLine - 1, lastLine).join('\n');
    const metadata = { sourceRef: ref(source), title: source.data.title, provenance: source.data.provenance,
      startLine, endLine: lastLine, sourceMetadata: source.data.sourceMetadata || null, digest: source.data.digest };
    const hasMoreLines = lastLine < lines.length;
    if (page === undefined) return { ...metadata, inlineText: selected.slice(0, 16000),
      truncated: selected.length > 16000 || !endLine && hasMoreLines };

    // Split by code point so a page boundary keeps every character intact.
    const characters = Array.from(selected);
    const offset = page * 16000;
    requireThat(startLine <= lastLine && (offset === 0 || offset < characters.length), 'SCHEMA_INVALID', 'Source page is out of range');
    const inlineText = characters.slice(offset, offset + 16000).join('');
    const displayStartLine = startLine + characters.slice(0, offset).filter(character => character === '\n').length;
    const hasMorePages = offset + 16000 < characters.length;
    return { ...metadata, inlineText, truncated: hasMorePages || !endLine && hasMoreLines,
      page, displayStartLine, hasMorePages, hasMoreLines };
  }
  async propose(input, actor) {
    requireThat(input.statement.trim() && (input.tags || []).every(tag => tag.trim()), 'SCHEMA_INVALID', 'Candidate and tags must be meaningful');
    requireThat((input.evidenceRanges || []).every(range => input.sourceIds.includes(range.sourceId))
      && new Set((input.evidenceRanges || []).map(range => range.sourceId)).size === (input.evidenceRanges || []).length,
    'SCHEMA_INVALID', 'Each evidence range must select one registered candidate source');
    return this.mutate(input, actor, document => {
      const previous = input.replaceProposalId && document.proposals.find(item => item.id === input.replaceProposalId);
      requireThat(!input.replaceProposalId || previous, 'REFERENCE_MISSING', 'Proposal to revise does not exist');
      const fragments = input.sourceIds.map(id => {
        const source = currentRecords(document, 'source').find(record => record.id === id);
        requireThat(source, 'REFERENCE_MISSING', 'Candidate must reference a source registered in this library');
        const range = input.evidenceRanges?.find(item => item.sourceId === id);
        if (!range) return currentRecords(document, 'fragment').find(record => record.data.sourceRef.id === id);
        const lines = source.data.inlineText.split(/\r?\n/);
        requireThat(range.startLine <= range.endLine && range.endLine <= lines.length, 'SCHEMA_INVALID', 'Evidence line range is invalid');
        const quote = lines.slice(range.startLine - 1, range.endLine).join('\n');
        requireThat(quote.trim() && quote.length <= 16000, 'SCHEMA_INVALID', 'Select a meaningful evidence excerpt of at most 16000 characters');
        return addRecord(document, 'fragment', { sourceRef: ref(source), location: { kind: 'line', value: `${range.startLine}-${range.endLine}` },
          quote, extraction: 'originalText', anchorStatus: 'matched', limitations: [] }, actor);
      });
      const knowledge = addRecord(document, 'knowledge', { kind: input.kind || 'practice', statement: input.statement,
        conditions: input.conditions || ['适用条件待用户审阅。'], scope: document.bundle.session.scope,
        exceptions: input.exceptions || [], sourceRefs: fragments.map(ref), provenance: 'analystReconstruction',
        lifecycle: 'candidate', epistemicStatus: 'unassessed', useDecision: 'referenceOnly', decisionRefs: [] }, actor,
      previous ? resolveRecord(document, previous.knowledgeRef, 'knowledge') : undefined);
      const proposal = { id: knowledge.id, knowledgeRef: ref(knowledge), tags: input.tags || [],
        status: 'pending', createdAt: now(), needsReassessment: false };
      proposal.subjectDigest = digestJson({ knowledgeDigest: semanticDigest(knowledge), tags: proposal.tags });
      if (previous) {
        document.proposalHistory ||= [];
        document.proposalHistory.push(structuredClone(previous));
        document.proposals = document.proposals.filter(item => item.id !== previous.id);
      }
      document.proposals.push(proposal);
      return { proposal: this.proposalView(document, proposal) };
    });
  }
  proposalView(document, proposal) {
    const record = resolveRecord(document, proposal.knowledgeRef, 'knowledge');
    return { id: proposal.id, status: proposal.status, statement: record.data.statement, kind: record.data.kind,
      conditions: record.data.conditions, exceptions: record.data.exceptions, sourceRefs: record.data.sourceRefs,
      tags: proposal.tags, subjectDigest: proposal.subjectDigest, needsReassessment: proposal.needsReassessment,
      sourceExcerpts: record.data.sourceRefs.map(reference => {
        const fragment = resolveRecord(document, reference, 'fragment');
        const source = resolveRecord(document, fragment.data.sourceRef, 'source');
        return { sourceId: source.id, title: source.data.title, location: fragment.data.location,
          quote: fragment.data.quote.slice(0, 2000), truncated: fragment.data.quote.length > 2000 };
      }) };
  }
  async decide(input, actor) {
    human(actor);
    return this.mutate(input, actor, document => this.applyDecision(document, input, actor));
  }
  async decideMany(input, actor) {
    human(actor);
    requireThat(new Set(input.decisions.map(item => item.proposalId)).size === input.decisions.length, 'SCHEMA_INVALID', 'A batch cannot repeat a proposal');
    return this.mutate(input, actor, document => ({ decisions: input.decisions.map(decision => this.applyDecision(document, decision, actor)) }));
  }
  applyDecision(document, input, actor) {
    const proposal = document.proposals.find(item => item.id === input.proposalId);
    requireThat(proposal && ['pending', 'deferred'].includes(proposal.status), 'REFERENCE_MISSING', 'Reviewable proposal is missing');
    requireThat(input.subjectDigest === proposal.subjectDigest, 'DECISION_SCOPE_MISMATCH', 'Candidate changed; inspect the current proposal');
    const base = resolveRecord(document, proposal.knowledgeRef, 'knowledge');
    const candidates = [base];
    if (input.verdict === 'adopt') {
      const tags = proposal.tags.map(name => currentRecords(document, 'tag').find(tag => tag.data.displayName === name && tag.data.status === 'accepted')
        || addRecord(document, 'tag', { facetId: 'topic', displayName: name,
          definition: `用户在此领域库采用的主题标签“${name}”。`, aliases: [], includeExamples: [], excludeExamples: [],
          status: 'proposed', decisionRefs: [] }, actor));
      candidates.push(...tags.filter(tag => tag.data.status === 'proposed'));
      if (tags.length) {
        // Accepted tag revisions are part of this same atomic repository save.
        candidates.push(addRecord(document, 'annotation', { targetRef: ref(base), anchorRefs: base.data.sourceRefs,
          assignments: tags.map(tag => ({ tagRef: { id: tag.id, revision: tag.revision + (tag.data.status === 'proposed' ? 1 : 0) },
            reason: '用户采用候选中的主题标签。', basisRefs: base.data.sourceRefs })),
          status: 'suggested', assignmentBasis: 'userDecision', decisionRefs: [] }, actor));
      }
    }
    const decision = addRecord(document, 'decision', {
      subjects: candidates.map(record => ({ ref: ref(record), contentDigest: semanticDigest(record) })),
      canonicalization: 'RFC8785', verdict: input.verdict, scope: base.data.scope,
      intendedUse: input.intendedUse || 'projectPractice', reason: input.reason || '用户在领域库审阅入口选择此操作。',
      basisRefs: base.data.sourceRefs }, actor);
    if (input.verdict === 'adopt') {
      for (const candidate of candidates) {
        const data = structuredClone(candidate.data);
        data.decisionRefs = [ref(decision)];
        if (candidate.recordType === 'knowledge') { data.lifecycle = 'confirmed'; data.useDecision = decision.data.intendedUse; }
        else data.status = 'accepted';
        const adopted = addRecord(document, candidate.recordType, data, actor, candidate);
        if (candidate.id === base.id) proposal.adoptedRef = ref(adopted);
      }
      proposal.status = 'adopted';
    } else proposal.status = input.verdict === 'defer' ? 'deferred' : 'rejected';
    proposal.decisionRef = ref(decision);
    return { proposalId: proposal.id, status: proposal.status, decisionRef: ref(decision),
      epistemicStatus: 'unassessed', intendedUse: decision.data.intendedUse };
  }
  async query({ libraryId, query = '', includeCandidates = false, tags = [] }) {
    checkLibraryContent(query);
    const document = await this.load(libraryId);
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const items = [];
    for (const knowledge of currentRecords(document, 'knowledge')) {
      if (includeCandidates ? ['quarantined', 'retracted', 'deprecated'].includes(knowledge.data.lifecycle) : knowledge.data.lifecycle !== 'confirmed') continue;
      const proposal = document.proposals.find(item => item.id === knowledge.id);
      if (!includeCandidates && (proposal?.needsReassessment || proposal?.status !== 'adopted')) continue;
      if (!tags.every(tag => proposal?.tags.includes(tag))) continue;
      const haystack = JSON.stringify({ statement: knowledge.data.statement, conditions: knowledge.data.conditions, tags: proposal?.tags }).toLowerCase();
      if (!terms.every(term => haystack.includes(term))) continue;
      items.push({ knowledgeRef: ref(knowledge), ...knowledge.data, tags: proposal?.tags || [], needsReassessment: !!proposal?.needsReassessment });
    }
    return { libraryId, revision: document.revision, items, total: items.length,
      queryMethod: 'lexical-and-adopted-tag-filter', storageTypeSelection: 'deferred' };
  }
  async research({ libraryId }) {
    const document = await this.load(libraryId);
    return { libraryId, objective: document.bundle.session.objective, scope: document.bundle.session.scope,
      questions: this.questions(document), gaps: currentRecords(document, 'gap').map(record => record.data),
      sources: currentRecords(document, 'source').map(record => ({ id: record.id, title: record.data.title, readCoverage: record.data.readCoverage })),
      method: ['围绕当前设计决定拆解概念、方法、操作、评价和边界。',
        '复原代表案例的问题、基线、动作、验证、结果、限制与迁移条件。',
        '核对原文与反例，区分观察、用户叙述、后续评价和AI归纳。',
        '通过source/propose保存有来源的候选，交用户选择用途。'],
      nextAction: '使用已登记材料及当前已授权的检索能力补充；外部资料使用前登记来源与读取范围。',
      scientificValidation: 'unassessed' };
  }
  async pause({ libraryId, paused, expectedRevision }, actor) {
    human(actor);
    const document = await this.load(libraryId);
    if (expectedRevision !== undefined) requireThat(document.revision === expectedRevision, 'REVISION_CONFLICT', 'Library changed');
    const previous = document.revision;
    document.bundle.session.status = paused ? 'paused' : 'active';
    document.revision++;
    document.bundle.session.revision = document.revision;
    validateDocument(document);
    await this.repository.save(document, previous);
    return this.summary(document);
  }
  async snapshot({ libraryId }) {
    const document = await this.load(libraryId);
    return { ...document, exportedAt: now(), snapshotDigest: digestJson(document) };
  }
}

