import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fixture } from './fixtures.js';
import { KnowledgeService, contentTypes } from '../packages/knowledge/index.js';
import { ImprovementService } from '../packages/improvement/index.js';
import { recordAuthorization } from '../packages/kernel/authorization.js';
import { serviceActor, id, digest } from '../packages/contracts/index.js';
import { inventoryAssets, applyImport, importLegacyState } from '../packages/migration/index.js';

test('knowledge preserves adjudication, scopes, origins and source invalidation', async t => {
  const f = await fixture(t);
  const knowledge = await KnowledgeService.open({ file: join(f.root, 'knowledge.sqlite'), workspace: f.root, store: f.store, evidence: f.evidence });
  t.after(() => knowledge.close());
  const file = join(f.root, 'lesson.md'); await writeFile(file, '高熵合金磨削 force fixed atoms mapping');
  const meta = await knowledge.ingest(file, { domain: 'hea', type: 'experience' });
  assert.equal(contentTypes.length, 13);
  assert.equal((await knowledge.search('磨削', { domain: 'hea' })).items.length, 0);
  const curator = { ...serviceActor('curator'), capabilities: ['knowledge.curate'] };
  const reviewer = { ...serviceActor('reviewer'), capabilities: ['knowledge.review'] };
  const proposal = knowledge.propose({ sourceId: meta.id, lesson: 'Exclude frozen atoms from thermal velocity generation', applicability: { domain: 'hea' } }, curator);
  assert.throws(() => knowledge.review(proposal.id, { verdict: 'confirmed', reasons: 'read' }, { ...curator, capabilities: ['knowledge.review'] }), { code: 'REVIEW_REQUIRED' });
  const review = knowledge.review(proposal.id, { verdict: 'confirmed', reasons: 'Specific correction with actual source' }, reviewer);
  assert.equal((await knowledge.apply(review.id)).status, 'confirmed');
  assert.equal((await knowledge.search('磨削', { domain: 'hea' })).items[0].status, 'confirmed');
  assert.equal((await knowledge.search('磨削', { domain: 'structural' })).items.length, 0);
  await writeFile(file, 'changed');
  assert.equal((await knowledge.search('磨削', { domain: 'hea' })).items.length, 0);
  assert.equal(knowledge.maintenance().status, 'reportOnly');
});
test('failure evidence can yield a candidate, quality regression blocks promotion, releases need exact grant', async t => {
  const f = await fixture(t), service = new ImprovementService({ store: f.store, root: join(f.root, 'improvement') });
  f.store.put('diagnoses', 'failure', { id: 'failure', status: 'failed' }, 0);
  const candidate = service.propose({ kind: 'role', assetKey: 'writer', content: 'Read matched local case before changing commands.', evidenceRefs: [{ kind: 'diagnoses', id: 'failure' }] });
  const cases = safe => [1,2].map(n => ({ id: `case-${n}`, candidate: { pass: true, scientificSafe: safe, permissionSafe: true, cost: .1 },
    baseline: { pass: true, scientificSafe: true, permissionSafe: true, cost: .2 } }));
  const regressed = await service.evaluate(candidate.id, { evaluator: async () => ({ cases: cases(false) }), evaluatorId: 'test/1', datasetVersion: '1' });
  assert.equal(regressed.eligible, false);
  await service.evaluate(candidate.id, { evaluator: async () => ({ cases: cases(true) }), evaluatorId: 'test/1', datasetVersion: '1' });
  await assert.rejects(service.apply(candidate.id, 'missing'), { code: 'AUTHORIZATION_REQUIRED' });
  const grant = recordAuthorization(f.store, { source: 'test explicit apply', actions: ['improvement.apply'], candidateDigest: candidate.contentDigest },
    { actorId: 'user', actorInstanceId: id('human'), kind: 'human' });
  const active = await service.apply(candidate.id, grant.id);
  assert.equal(active.contentDigest, candidate.contentDigest);
  assert.equal(f.store.get('diagnoses', 'failure').status, 'failed');
  await assert.rejects(service.apply(candidate.id, grant.id), { code: 'STATE_CONFLICT' });
});
test('inventory/import is idempotent and preserves unknown historical provenance', async t => {
  const f = await fixture(t);
  const source = join(f.root, 'source'); await mkdir(source);
  await writeFile(join(source, 'input.md'), 'actual source');
  const inventory = await inventoryAssets(source, ['input.md', 'missing.md'], 'frozen-commit');
  const args = { sourceRoot: source, targetRoot: join(f.root, 'import'), inventory, store: f.store };
  assert.equal((await applyImport(args)).wouldCopy, 1);
  const imported = await applyImport({ ...args, dryRun: false });
  assert.equal(imported.imported.length, 1);
  assert.equal((await applyImport({ ...args, dryRun: false })).id, imported.id);
  assert.equal(importLegacyState({ verdict: 'not_met' }, 'legacy').verdict, 'notMet');
  assert.equal(importLegacyState({ status: 'PASS' }, 'legacy').provenanceStatus, 'imported_unknown');
});
