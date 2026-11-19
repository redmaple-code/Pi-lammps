import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { digest, id, utc, requireCondition } from '../contracts/index.js';
import { hashFile, scopedPath } from '../artifacts/index.js';
import { authorize } from '../kernel/authorization.js';
import { validatePackStructure } from '../contracts/validation.js';

export function scientificContentDigest(pack) {
  const candidate = structuredClone(pack);
  const receiptId = candidate.releaseReceiptRef?.split('#')[0];
  delete candidate.publicationStatus; delete candidate.releaseReceiptRef;
  candidate.resources = candidate.resources.filter(resource => resource.id !== receiptId);
  candidate.entrypoints.validation = candidate.entrypoints.validation.filter(reference => reference.split('#')[0] !== receiptId);
  return digest(candidate);
}
export async function publishPack({ manifestPath, targetRoot, store, authorizationRef, verificationRefs }) {
  const pack = validatePackStructure(JSON.parse(await readFile(manifestPath, 'utf8')));
  const scientificDigest = scientificContentDigest(pack);
  authorize(store, authorizationRef, 'pack.publish', { candidateDigest: scientificDigest });
  const previous = store.get('package-releases', scientificDigest);
  if (previous?.status === 'released') return previous;
  requireCondition(verificationRefs.length > 0, 'EVIDENCE_INSUFFICIENT', 'Package release needs actual validation receipts');
  const verifications = verificationRefs.map(ref => store.get('package-verifications', ref));
  requireCondition(verifications.every(receipt => receipt?.scientificDigest === scientificDigest && receipt.verdict === 'passed'
    && receipt.positiveCaseRefs?.length && receipt.negativeCaseRefs?.length && receipt.reviewerRef), 'EVIDENCE_INSUFFICIENT', 'Validation receipts do not match scientific content');
  for (const profile of pack.toolProfiles) for (const binding of profile.bindings) {
    requireCondition(binding.validationStatus === 'validated' && binding.validationReceiptRef, 'EVIDENCE_INSUFFICIENT', 'Tool bindings require validated compatibility receipts before release');
  }
  const destination = join(targetRoot, pack.id, `${pack.version}-${scientificDigest.slice(7)}`);
  const release = previous || { id: id('pack-release'), scientificDigest, digestAlgorithm: 'canonical-json-v1/scientific-closure',
    packId: pack.id, version: pack.version, verificationRefs, authorizationRef, createdAt: utc() };
  if (!previous) store.put('package-releases', scientificDigest, { ...release, status: 'preparing' }, 0);
  async function writeImmutable(path, value) {
    const text = JSON.stringify(value, null, 2);
    try { await writeFile(path, text, { flag: 'wx' }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; requireCondition(await readFile(path, 'utf8') === text, 'ARTIFACT_CHANGED', 'Archive file differs'); }
  }
  await mkdir(destination, { recursive: true });
  for (const resource of pack.resources) {
    requireCondition(resource.availability === 'available', 'REFERENCE_MISSING', 'Cannot release missing resources');
    const source = await scopedPath(dirname(manifestPath), resource.path);
    requireCondition(await hashFile(source) === resource.digest, 'ARTIFACT_CHANGED', 'Resource changed before publication');
    const target = await scopedPath(destination, resource.path, { mustExist: false });
    await mkdir(dirname(target), { recursive: true });
    try { await copyFile(source, target, 1); }
    catch (error) { if (error.code !== 'EEXIST') throw error; requireCondition(await hashFile(target) === resource.digest, 'ARTIFACT_CHANGED', 'Archive target differs'); }
  }
  const receiptPath = 'release-receipt.json';
  const publicRelease = { id: release.id, scientificDigest, digestAlgorithm: release.digestAlgorithm, packId: pack.id,
    version: pack.version, verificationRefs: release.verificationRefs, authorizationRef: release.authorizationRef, createdAt: release.createdAt };
  await writeImmutable(join(destination, receiptPath), { items: [publicRelease] });
  pack.resources.push({ id: 'release-receipt', kind: 'validation', path: receiptPath, availability: 'available', digest: await hashFile(join(destination, receiptPath)) });
  pack.publicationStatus = 'released'; pack.releaseReceiptRef = `release-receipt#${release.id}`;
  pack.entrypoints.validation.push('release-receipt');
  validatePackStructure(pack);
  const archiveDigest = digest(pack);
  await writeImmutable(join(destination, 'manifest.json'), pack);
  store.put('package-releases', scientificDigest, { ...publicRelease, archiveDigest, manifestPath: join(destination, 'manifest.json'), status: 'released' },
    store.get('package-releases', scientificDigest).recordVersion);
  return store.get('package-releases', scientificDigest);
}
export function withdrawPack(store, scientificDigest, authorizationRef, reason) {
  authorize(store, authorizationRef, 'pack.withdraw', { candidateDigest: scientificDigest });
  const release = store.get('package-releases', scientificDigest);
  requireCondition(release && reason, 'REFERENCE_MISSING', 'Release and withdrawal reason required');
  return store.put('package-releases', scientificDigest, { ...release, status: 'withdrawn', reason, withdrawnAt: utc() }, release.recordVersion);
}
