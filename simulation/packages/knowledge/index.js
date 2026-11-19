import { PlatformState } from '@paperpilot/agent-base/platform';
import { id, digest, utc, requireCondition } from '../contracts/index.js';
import { authorize } from '../kernel/authorization.js';
import { hashFile, scopedPath } from '../artifacts/index.js';
import { readFile, writeFile, rename, open, unlink } from 'node:fs/promises';

export const contentTypes = ['case', 'case_note', 'correction', 'dialogue', 'error', 'experience', 'input_script',
  'output_log', 'potential_note', 'qa', 'rule', 'template_snippet', 'unknown'];
export class KnowledgeService {
  static async open({ file, workspace, store, evidence, aliases = {} }) {
    const platform = await PlatformState.open(file, workspace);
    platform.knowledgeOptions = { aliases };
    return new KnowledgeService(platform, store, evidence);
  }
  constructor(platform, store, evidence) { Object.assign(this, { platform, store, evidence }); }
  async ingest(path, { domain, type, title, originGroupId, status = 'candidate', legacy } = {}) {
    requireCondition(contentTypes.includes(type) && domain, 'SCHEMA_INVALID', 'Knowledge needs domain and supported content type');
    requireCondition(['candidate', 'pending_review', 'quarantined'].includes(status), 'REVIEW_REQUIRED', 'New content requires adjudication');
    const imported = await this.platform.knowledge.importFile(path, { title });
    const key = imported.id;
    const prior = this.store.get('knowledge-meta', key);
    const meta = { id: key, domain, type, title: title || path, status, quality: 'uncertain', originGroupId: originGroupId || key,
      reference: imported.reference, locator: imported.path, sourceDigest: await hashFile(imported.path), createdAt: utc(), legacy: legacy || null };
    this.store.put('knowledge-meta', key, meta, prior?.recordVersion || 0);
    return meta;
  }
  async search(query, { domain, includeCandidates = false, limit = 8 } = {}) {
    const result = await this.platform.knowledge.search(query, { limit: 50 });
    const seen = new Set(), items = [];
    for (const item of result.items) {
      const meta = this.store.get('knowledge-meta', item.sourceId);
      if (!meta || (domain && meta.domain !== domain) || (!includeCandidates && meta.status !== 'confirmed') || meta.status === 'quarantined') continue;
      if (seen.has(meta.originGroupId)) continue;
      seen.add(meta.originGroupId);
      items.push({ ...item, domain: meta.domain, type: meta.type, status: meta.status, quality: meta.quality,
        originGroupId: meta.originGroupId, whyRanked: ['lexical-match', 'domain-filter', 'origin-diversity'],
        evidenceLines: { start: item.citation.startLine, end: item.citation.endLine }, locator: meta.locator,
        sourceKind: ['case', 'input_script', 'case_note'].includes(meta.type) ? 'case' : meta.type === 'correction' ? 'correction' : 'memory' });
      if (items.length >= limit) break;
    }
    return { ...result, items, total: items.length, totalIsExact: false, scope: domain || 'all-explicitly-selected' };
  }
  async read(sourceId, scope, actor) {
    const meta = this.store.get('knowledge-meta', sourceId);
    requireCondition(meta, 'REFERENCE_MISSING', 'Knowledge source missing');
    await this.platform.knowledge.validateSource(meta.reference);
    return this.evidence.read({ ...meta, sourceId, sourceKind: ['case', 'input_script', 'case_note'].includes(meta.type) ? 'case' : 'memory' }, scope, actor);
  }
  propose({ sourceId, lesson, applicability, target, mergeMode = 'append', expectedDigest, targetSection }, curator) {
    requireCondition(curator.capabilities?.includes('knowledge.curate') && lesson?.trim() && applicability, 'PERMISSION_DENIED', 'Curator and applicable lesson required');
    const meta = this.store.get('knowledge-meta', sourceId);
    requireCondition(meta, 'REFERENCE_MISSING', 'Missing source');
    const proposal = { id: id('kb-proposal'), sourceId, sourceDigest: meta.sourceDigest, lesson, applicability,
      target: target || null, targetSection: targetSection || null, mergeMode, expectedDigest: expectedDigest || null, curatorInstanceId: curator.actorInstanceId,
      status: 'pending_review', createdAt: utc() };
    this.store.put('knowledge-proposals', proposal.id, proposal, 0);
    return proposal;
  }
  review(proposalId, { verdict, reasons, quality = 'useful' }, reviewer) {
    const proposal = this.store.get('knowledge-proposals', proposalId);
    requireCondition(reviewer.capabilities?.includes('knowledge.review') && reviewer.actorInstanceId !== proposal?.curatorInstanceId,
      'REVIEW_REQUIRED', 'Independent knowledge reviewer required');
    requireCondition(['confirmed', 'candidate', 'quarantined'].includes(verdict) && reasons?.trim(), 'SCHEMA_INVALID', 'Knowledge verdict and reason required');
    const receipt = { id: id('kb-review'), proposalId, proposalDigest: digest({ lesson: proposal.lesson, sourceId: proposal.sourceId }),
      verdict, reasons, quality, reviewer, createdAt: utc() };
    this.store.put('knowledge-reviews', receipt.id, receipt, 0);
    return receipt;
  }
  async apply(reviewId) {
    const review = this.store.get('knowledge-reviews', reviewId), proposal = review && this.store.get('knowledge-proposals', review.proposalId);
    requireCondition(proposal && review.proposalDigest === digest({ lesson: proposal.lesson, sourceId: proposal.sourceId }), 'REVIEW_REQUIRED', 'Proposal changed after review');
    const meta = this.store.get('knowledge-meta', proposal.sourceId);
    requireCondition(await hashFile(meta.locator) === proposal.sourceDigest, 'ARTIFACT_CHANGED', 'Knowledge source changed');
    if (proposal.status === 'applied') return meta;
    const applied = this.store.put('knowledge-meta', meta.id, { ...meta, status: review.verdict, quality: review.quality,
      lesson: proposal.lesson, applicability: proposal.applicability, reviewRef: review.id }, meta.recordVersion);
    this.store.put('knowledge-proposals', proposal.id, { ...proposal, status: 'applied', reviewRef: review.id }, proposal.recordVersion);
    return applied;
  }
  async mergeSection(proposalId, authorizationRef) {
    const proposal = this.store.get('knowledge-proposals', proposalId);
    requireCondition(proposal?.status === 'applied' && proposal.target, 'REVIEW_REQUIRED', 'Adjudicated merge proposal required');
    authorize(this.store, authorizationRef, 'knowledge.merge', { candidateDigest: digest(proposal.lesson) });
    const target = await scopedPath(this.platform.workspace, proposal.target);
    const lockPath = `${target}.redmaple-merge.lock`;
    const lock = await open(lockPath, 'wx');
    try {
      const old = await readFile(target, 'utf8');
      const marker = `<!-- lesson:${proposal.id} -->`;
      if (old.includes(marker)) return { status: 'alreadyApplied' };
      requireCondition(await hashFile(target) === proposal.expectedDigest, 'ARTIFACT_CHANGED', 'Merge target changed');
      let updated;
      if (proposal.mergeMode === 'append') updated = `${old}\n\n${marker}\n${proposal.lesson}\n`;
      else {
        requireCondition(['enhance', 'replace'].includes(proposal.mergeMode) && proposal.targetSection,
          'SCHEMA_INVALID', 'Bounded enhancement/replacement needs a target section');
        const start = old.indexOf(proposal.targetSection);
        requireCondition(start >= 0 && old.indexOf(proposal.targetSection, start + 1) < 0, 'STATE_CONFLICT', 'Section is missing or ambiguous');
        const nextHeading = old.slice(start + proposal.targetSection.length).search(/\n#{1,6}\s/);
        const end = nextHeading < 0 ? old.length : start + proposal.targetSection.length + nextHeading;
        const section = old.slice(start, end);
        const replacement = proposal.mergeMode === 'enhance' ? `${section}\n\n${marker}\n${proposal.lesson}\n` : `${proposal.targetSection}\n\n${marker}\n${proposal.lesson}\n`;
        updated = old.slice(0, start) + replacement + old.slice(end);
      }
      const backup = `${target}.${proposal.id}.backup`;
      await writeFile(backup, old, { flag: 'wx' });
      const temporary = `${target}.${proposal.id}.tmp`;
      await writeFile(temporary, updated, { flag: 'wx' });
      requireCondition(await hashFile(target) === proposal.expectedDigest, 'ARTIFACT_CHANGED', 'Merge target changed during write');
      await rename(temporary, target);
      return { status: 'applied', backup, digest: await hashFile(target) };
    } finally { await lock.close(); await unlink(lockPath); }
  }
  remember({ category, key, text, sources }) {
    requireCondition(['user', 'feedback', 'project', 'reference'].includes(category), 'SCHEMA_INVALID', 'Invalid memory category');
    return this.platform.remember({ key: `${category}/${key}`, text, sources });
  }
  maintenance() {
    const entries = this.store.list('knowledge-meta');
    return { status: 'reportOnly', byStatus: entries.reduce((result, item) => ({ ...result, [item.status]: (result[item.status] || 0) + 1 }), {}),
      candidates: entries.filter(item => item.status !== 'confirmed').map(item => item.id) };
  }
  async backup(file) { return this.platform.backupTo(file); }
  close() { this.platform.close(); }
}
