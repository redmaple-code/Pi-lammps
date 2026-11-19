import { readFile } from 'node:fs/promises';
import { hashFile } from '../artifacts/index.js';
import { id, utc, requireCondition } from '../contracts/index.js';

export class EvidenceService {
  constructor(store) { this.store = store; }
  async read(source, { startLine = 1, endLine, representation = 'section' } = {}, actor) {
    requireCondition(actor?.actorInstanceId, 'IDENTITY_REQUIRED', 'Reader identity required');
    const text = await readFile(source.locator, 'utf8');
    const lines = text.split(/\r?\n/);
    const last = endLine ?? lines.length;
    requireCondition(startLine >= 1 && last >= startLine && last <= lines.length, 'SCHEMA_INVALID', 'Invalid source read range');
    if (representation === 'fullText') requireCondition(startLine === 1 && last === lines.length, 'SCHEMA_INVALID', 'Full-text receipt requires complete text');
    const actual = await hashFile(source.locator);
    if (source.digest) requireCondition(source.digest === actual, 'ARTIFACT_CHANGED', 'Evidence source changed');
    const receipt = { id: id('read'), sourceId: source.sourceId, sourceKind: source.sourceKind,
      originGroupId: source.originGroupId, locator: source.locator, digest: actual,
      representation, startLine, endLine: last, totalLines: lines.length, accessedAt: utc(), reader: actor,
      applicability: source.applicability || {}, status: source.status || 'candidate',
      excerpt: lines.slice(startLine - 1, last).join('\n') };
    this.store.put('reads', receipt.id, receipt, 0);
    return receipt;
  }
  async assess(readRefs, policy = {}, readerInstanceId, claimEvidence = []) {
    const receipts = [];
    for (const ref of readRefs) {
      const receipt = this.store.get('reads', typeof ref === 'string' ? ref : ref.id);
      requireCondition(receipt, 'EVIDENCE_INSUFFICIENT', 'No actual source read receipt');
      requireCondition(!readerInstanceId || receipt.reader.actorInstanceId === readerInstanceId, 'EVIDENCE_INSUFFICIENT', 'Evidence was not read by this reviewer');
      requireCondition(await hashFile(receipt.locator) === receipt.digest, 'ARTIFACT_CHANGED', 'Evidence changed');
      receipts.push(receipt);
    }
    const reasons = [];
    if (policy.localCaseRequired && !receipts.some(r => r.sourceKind === 'case' && ['section', 'fullText'].includes(r.representation))) reasons.push('localCaseRequired');
    if (policy.fullTextRequired && !receipts.some(r => r.representation === 'fullText')) reasons.push('fullTextRequired');
    if (policy.dualEvidence) {
      const manuals = receipts.filter(r => ['manual', 'correction'].includes(r.sourceKind));
      const cases = receipts.filter(r => ['case', 'memory'].includes(r.sourceKind));
      if (!manuals.some(a => cases.some(b => a.originGroupId && b.originGroupId && a.originGroupId !== b.originGroupId))) reasons.push('independentDualEvidenceRequired');
    }
    const claims = (policy.claims || []).map(claim => {
      const bindings = claimEvidence.filter(binding => binding.claimId === claim.id);
      const relevant = bindings.filter(binding => {
        const read = receipts.find(item => item.id === binding.readRef);
        return read && claim.sourceIds.includes(read.sourceId) && (!claim.sourceDigests || claim.sourceDigests.includes(read.digest)) && typeof binding.quote === 'string'
          && binding.quote.trim().length > 0 && read.excerpt.replace(/\s+/g, ' ').includes(binding.quote.replace(/\s+/g, ' '))
          && typeof binding.reason === 'string' && binding.reason.trim().length > 0;
      });
      const contradicted = relevant.some(binding => binding.judgment === 'contradicts');
      const supported = !contradicted && relevant.some(binding => binding.judgment === 'supports');
      if (!supported && claim.blocking !== false) reasons.push(`claimUnsupported:${claim.id}`);
      return { claimId: claim.id, supported, contradicted, blocking: claim.blocking !== false };
    });
    return { passed: reasons.length === 0, reasons, claims, readRefs: receipts.map(r => r.id), originGroups: [...new Set(receipts.map(r => r.originGroupId))] };
  }
}
