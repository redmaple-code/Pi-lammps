import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fixture } from './fixtures.js';
import { id, digest } from '../packages/contracts/index.js';
import { recordAuthorization } from '../packages/kernel/authorization.js';
import { SimulationStore } from '../packages/kernel/state.js';
import { diagnoseFailure, planRepair } from '../packages/kernel/repair.js';
import { UKBKnowledgeAdapter, KnowledgeBackendRegistry } from '../packages/knowledge/backends.js';
import { scientificContentDigest, publishPack, withdrawPack } from '../packages/pack-resolver/release.js';
import { simulationRoot, buildPacks } from '../scripts/build-packs.js';
import { resolvePack } from '../packages/pack-resolver/index.js';

const user = () => ({ actorId: 'test-user', actorInstanceId: id('human'), kind: 'human' });
test('consistent SQLite backup retains committed events and independent repair counters', async t => {
  const f = await fixture(t);
  const diagnosis = diagnoseFailure({ runRef: 'actual-failed-run', log: 'ERROR: Lost atoms', readScope: { start: 1, end: 1 } });
  assert.equal(diagnosis.signals[0].earliestStage, 'undetermined');
  const repair = planRepair(diagnosis, { hypothesis: 'structure issue', evidenceRefs: ['geometry'], changes: ['inspect overlap'], earliestStage: 'structure', requiredReviews: ['structure'], chainId: 'same-chain' });
  for (let attempt = 0; attempt < 3; attempt++) await f.kernel.command(f.state.id, 'repair', { chainId: repair.chainId, success: false, attempt });
  await assert.rejects(f.kernel.command(f.state.id, 'repair', { chainId: repair.chainId, success: false }), { code: 'REPAIR_LIMIT' });
  const backup = join(f.root, 'backup.sqlite'); await f.store.backup(backup);
  const restored = new SimulationStore(backup); t.after(() => restored.close());
  assert.equal(restored.getCase(f.state.id).repairs['same-chain'].failures, 3);
  assert.equal(restored.getCase(f.state.id).stages[0].consecutiveRevisions, 0);
  assert.equal(restored.events(f.state.id).length, f.store.events(f.state.id).length);
});
test('case revisions and invalidation preserve earlier terminal evidence', async t => {
  const f = await fixture(t, { stages: [{ id: 'write', needs: [], reviewRequired: false }] });
  const ref = await f.artifacts.text('original');
  await f.kernel.command(f.state.id, 'start', { stageId: 'write', producerInstanceId: 'writer' });
  f.store.put('completions', 'done', { id: 'done' }, 0);
  await f.kernel.command(f.state.id, 'produced', { stageId: 'write', outputRefs: [ref], completionReceiptRef: 'done' });
  const original = f.store.getCase(f.state.id);
  const next = f.store.forkRevision(f.state.id, { workflow: original.workflow, lock: original.lock, authorization: null, reason: 'new physical conditions' });
  assert.equal(f.store.caseRevision(f.state.id, original.revisionId).stages[0].status, 'passed');
  assert.equal(next.stages[0].status, 'pending');
  assert.notEqual(next.revisionId, original.revisionId);
});
test('UKB capability limitations stay visible and proposal status cannot publish', async () => {
  const client = { search: async () => ({ items: [{ id: 'entry' }] }), read: async () => ({ content: 'source' }),
    validate: async () => ({ status: 'valid' }), propose: async () => ({ id: 'proposal', status: 'confirmed' }) };
  const backend = new UKBKnowledgeAdapter(client);
  const registry = new KnowledgeBackendRegistry(); registry.register('ukb', backend);
  assert.equal((await registry.get('ukb').search('x')).items[0].provenanceStatus, 'unfrozen');
  assert.equal((await backend.validate('entry')).status, 'unfrozen');
  assert.equal((await backend.propose({})).status, 'candidate');
});
test('pack publication needs frozen positive/negative evidence and host release registration', async t => {
  const f = await fixture(t); await buildPacks();
  const originalPath = join(simulationRoot, 'domains/lammps/manifest.json');
  const pack = JSON.parse(await readFile(originalPath, 'utf8'));
  const candidateRoot = join(f.root, 'candidate'); await mkdir(candidateRoot);
  for (const resource of pack.resources) {
    await mkdir(dirname(join(candidateRoot, resource.path)), { recursive: true });
    await copyFile(join(dirname(originalPath), resource.path), join(candidateRoot, resource.path));
  }
  pack.toolProfiles[0].bindings[0].validationStatus = 'validated';
  pack.toolProfiles[0].bindings[0].validationReceiptRef = 'validation#grinding';
  const manifestPath = join(candidateRoot, 'manifest.json');
  await writeFile(manifestPath, JSON.stringify(pack));
  const scientificDigest = scientificContentDigest(pack);
  const grant = recordAuthorization(f.store, { source: 'explicit test package publication', actions: ['pack.publish', 'pack.withdraw'], candidateDigest: scientificDigest }, user());
  await assert.rejects(publishPack({ manifestPath, targetRoot: join(f.root, 'published'), store: f.store, authorizationRef: grant.id, verificationRefs: [] }), { code: 'EVIDENCE_INSUFFICIENT' });
  f.store.put('package-verifications', 'fixture-verification', { id: 'fixture-verification', scientificDigest, verdict: 'passed',
    positiveCaseRefs: ['fixture-positive'], negativeCaseRefs: ['fixture-negative'], reviewerRef: 'test-reviewer' }, 0);
  const release = await publishPack({ manifestPath, targetRoot: join(f.root, 'published'), store: f.store,
    authorizationRef: grant.id, verificationRefs: ['fixture-verification'] });
  const released = JSON.parse(await readFile(release.manifestPath, 'utf8'));
  assert.equal(scientificContentDigest(released), scientificDigest);
  const registry = { 'local-lammps': { version: '0.1.0', capabilities: ['molecular.dynamics.solve'] } };
  const resolved = await resolvePack(release.manifestPath, { capabilityRegistry: registry, releaseLookup: hash => f.store.get('package-releases', hash) });
  assert.equal(resolved.executionReady, true);
  assert.equal((await resolvePack(release.manifestPath, { capabilityRegistry: registry })).executionReady, false, 'Unregistered release metadata is insufficient');
  withdrawPack(f.store, scientificDigest, grant.id, 'test withdrawal');
  assert.equal(f.store.get('package-releases', scientificDigest).status, 'withdrawn');
});
