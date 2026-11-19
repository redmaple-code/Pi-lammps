import { mkdir, readFile, writeFile, rename, copyFile, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { id, digest, utc, requireCondition } from '../contracts/index.js';
import { hashFile } from '../artifacts/index.js';
import { authorize } from '../kernel/authorization.js';

export class ImprovementService {
  constructor({ store, root }) { Object.assign(this, { store, root }); }
  propose({ kind, assetKey, content, evidenceRefs, target = null, historyScope = null }) {
    requireCondition(['skill', 'role', 'strategy', 'memory', 'domain'].includes(kind) && assetKey && content?.trim() && evidenceRefs?.length,
      'SCHEMA_INVALID', 'Candidate requires content, target kind and evidence');
    for (const ref of evidenceRefs) requireCondition(this.store.get(ref.kind, ref.id), 'EVIDENCE_INSUFFICIENT', 'Candidate evidence is missing');
    const candidateId = digest({ kind, assetKey, content, evidenceRefs }).slice(7);
    const prior = this.store.get('improvements', candidateId);
    if (prior) return prior;
    const candidate = { id: candidateId, kind, assetKey, content, contentDigest: digest(content), evidenceRefs, target, historyScope,
      status: 'proposed', humanReviewRequired: true, autoApplyAllowed: false, createdAt: utc() };
    this.store.put('improvements', candidate.id, candidate, 0);
    return candidate;
  }
  mine(authorizedHistory, grant) {
    authorize(this.store, grant.authorizationRef, 'history.read');
    requireCondition(authorizedHistory.every(item => grant.allowedSessionIds.includes(item.sessionId)), 'PERMISSION_DENIED', 'History exceeds authorized sessions');
    const groups = new Map();
    for (const item of authorizedHistory) {
      const key = item.task.trim().toLowerCase();
      groups.set(key, [...(groups.get(key) || []), { sessionId: item.sessionId, evidenceRef: item.evidenceRef }]);
    }
    return [...groups].filter(([, items]) => items.length >= 2).map(([task, items]) => ({ task, frequency: items.length, evidenceRefs: items.map(item => item.evidenceRef) }));
  }
  async evaluate(candidateId, { evaluator, datasetVersion, evaluatorId, minCases = 2, maxCost = 1, signal }) {
    const candidate = this.store.get('improvements', candidateId);
    requireCondition(candidate && typeof evaluator === 'function' && datasetVersion && evaluatorId, 'SCHEMA_INVALID', 'Versioned evaluator required');
    const active = this.store.get('active-assets', candidate.assetKey);
    const checkpoint = this.store.get('evaluation-checkpoints', candidateId);
    const result = await evaluator({ candidate, baseline: active, signal, previous: checkpoint?.value,
      checkpoint: value => this.store.put('evaluation-checkpoints', candidateId, { value }) });
    requireCondition(result.cases?.length >= minCases && new Set(result.cases.map(item => item.id)).size === result.cases.length, 'SCHEMA_INVALID', 'Independent paired cases required');
    for (const item of result.cases) for (const version of ['candidate', 'baseline']) {
      const record = item[version];
      requireCondition(typeof record?.pass === 'boolean' && Number.isFinite(record.cost) && record.cost >= 0
        && typeof record.scientificSafe === 'boolean' && typeof record.permissionSafe === 'boolean', 'SCHEMA_INVALID', 'Evaluation needs quality and cost per pair');
    }
    const noRegression = result.cases.every(item => (!item.baseline.pass || item.candidate.pass) && item.candidate.scientificSafe && item.candidate.permissionSafe);
    const passGain = result.cases.filter(item => item.candidate.pass).length - result.cases.filter(item => item.baseline.pass).length;
    const candidateCost = result.cases.reduce((sum, item) => sum + item.candidate.cost, 0);
    const baselineCost = result.cases.reduce((sum, item) => sum + item.baseline.cost, 0);
    const eligible = noRegression && candidateCost + baselineCost <= maxCost && (passGain > 0 || (passGain === 0 && candidateCost < baselineCost));
    const receipt = { id: id('evaluation'), candidateId, contentDigest: candidate.contentDigest, baselineDigest: active?.contentDigest || null,
      datasetVersion, evaluatorId, cases: result.cases, eligible, candidateCost, baselineCost, createdAt: utc() };
    this.store.put('evaluations', receipt.id, receipt, 0);
    this.store.put('improvements', candidateId, { ...candidate, evaluationRef: receipt.id, status: eligible ? 'eligible' : 'evaluated' }, candidate.recordVersion);
    return receipt;
  }
  async stage(candidateId) {
    const candidate = this.store.get('improvements', candidateId);
    requireCondition(candidate, 'REFERENCE_MISSING', 'Candidate missing');
    const directory = join(this.root, 'candidates', candidateId);
    await mkdir(directory, { recursive: true });
    const path = join(directory, candidate.kind === 'skill' ? 'SKILL.md' : 'candidate.md');
    try { await writeFile(path, candidate.content, { flag: 'wx' }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; requireCondition(await readFile(path, 'utf8') === candidate.content, 'ARTIFACT_CHANGED', 'Staged candidate changed'); }
    return { directory, path, contentDigest: candidate.contentDigest };
  }
  async apply(candidateId, authorizationRef, { expectedVersion = 0 } = {}) {
    const candidate = this.store.get('improvements', candidateId), evaluation = candidate && this.store.get('evaluations', candidate.evaluationRef);
    requireCondition(candidate?.status === 'eligible' && evaluation?.eligible && evaluation.contentDigest === candidate.contentDigest, 'EVALUATION_REQUIRED', 'Candidate needs current paired evaluation');
    authorize(this.store, authorizationRef, 'improvement.apply', { candidateDigest: candidate.contentDigest });
    const current = this.store.get('active-assets', candidate.assetKey);
    requireCondition((current?.recordVersion || 0) === expectedVersion && (current?.contentDigest || null) === evaluation.baselineDigest, 'STATE_CONFLICT', 'Active baseline changed');
    await this.stage(candidateId);
    const value = { assetKey: candidate.assetKey, candidateId, content: candidate.content, contentDigest: candidate.contentDigest,
      previous: current || null, evaluationRef: evaluation.id, authorizationRef, appliedAt: utc() };
    return this.store.put('active-assets', candidate.assetKey, value, expectedVersion);
  }
  rollback(assetKey, authorizationRef, expectedVersion) {
    authorize(this.store, authorizationRef, 'improvement.rollback');
    const current = this.store.get('active-assets', assetKey);
    requireCondition(current?.recordVersion === expectedVersion, 'STATE_CONFLICT', 'Active asset changed');
    return this.store.put('active-assets', assetKey, { ...(current.previous || { assetKey, content: '', contentDigest: null }),
      rollbackOf: current.candidateId, rolledBackAt: utc() }, expectedVersion);
  }
  async installSkill(candidateId, target, authorizationRef, expectedDigest = null) {
    const candidate = this.store.get('improvements', candidateId);
    requireCondition(candidate?.kind === 'skill' && candidate.status === 'eligible', 'EVALUATION_REQUIRED', 'Evaluated skill required');
    authorize(this.store, authorizationRef, 'skill.install', { candidateDigest: candidate.contentDigest });
    let existing = null;
    try { existing = await hashFile(target); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    requireCondition(existing === expectedDigest, 'ARTIFACT_CHANGED', 'Skill target changed');
    const temporary = `${target}.${candidateId}.tmp`;
    await writeFile(temporary, candidate.content, { flag: 'wx' });
    if (existing) await copyFile(target, `${target}.${candidateId}.backup`);
    if (!existing) { await writeFile(target, candidate.content, { flag: 'wx' }); await unlink(temporary); }
    else { requireCondition(await hashFile(target) === existing, 'ARTIFACT_CHANGED', 'Skill changed during install'); await rename(temporary, target); }
    const receipt = { id: id('skill-release'), candidateId, target, installedDigest: await hashFile(target), previousDigest: existing,
      backup: existing ? `${target}.${candidateId}.backup` : null, authorizationRef, createdAt: utc() };
    this.store.put('skill-releases', receipt.id, receipt, 0);
    return receipt;
  }
  async uninstallSkill(releaseId, authorizationRef) {
    authorize(this.store, authorizationRef, 'skill.uninstall');
    const receipt = this.store.get('skill-releases', releaseId);
    requireCondition(receipt, 'REFERENCE_MISSING', 'Skill release missing');
    requireCondition(await hashFile(receipt.target) === receipt.installedDigest, 'ARTIFACT_CHANGED', 'Skill has user edits after installation');
    if (receipt.backup) {
      requireCondition(await hashFile(receipt.backup) === receipt.previousDigest, 'ARTIFACT_CHANGED', 'Skill backup changed');
      await rename(receipt.backup, receipt.target);
    } else await unlink(receipt.target);
    return this.store.put('skill-releases', releaseId, { ...receipt, status: 'uninstalled', uninstalledAt: utc() }, receipt.recordVersion);
  }
}
