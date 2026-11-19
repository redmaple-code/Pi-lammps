import { stat, mkdir, copyFile, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { digest, utc, requireCondition, migrateLegacy } from '../contracts/index.js';
import { hashFile, scopedPath } from '../artifacts/index.js';

export async function inventoryAssets(root, declaredPaths, sourceCommit) {
  const entries = [];
  for (const path of declaredPaths) {
    try {
      const source = await scopedPath(root, path);
      if (!(await stat(source)).isFile()) continue;
      entries.push({ sourcePath: path, digest: await hashFile(source), byteLength: (await stat(source)).size,
        disposition: 'copy', targetPath: path.replace(/^\.angsheng\//, 'legacy/'), status: 'available' });
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      entries.push({ sourcePath: path, disposition: 'restoreRequired', status: 'missing' });
    }
  }
  return { sourceCommit, entries, inventoryDigest: digest(entries), createdAt: utc() };
}
export async function applyImport({ sourceRoot, targetRoot, inventory, store, dryRun = true }) {
  const batchId = digest({ sourceCommit: inventory.sourceCommit, inventoryDigest: inventory.inventoryDigest, targetRoot }).slice(7);
  const prior = store.get('imports', batchId);
  if (prior?.status === 'completed') return prior;
  const report = { id: batchId, status: dryRun ? 'dryRun' : 'running', sourceCommit: inventory.sourceCommit,
    imported: [], missing: inventory.entries.filter(entry => entry.status === 'missing'), createdAt: utc() };
  if (dryRun) return { ...report, wouldCopy: inventory.entries.filter(entry => entry.status === 'available').length };
  await mkdir(targetRoot, { recursive: true });
  for (const entry of inventory.entries.filter(entry => entry.status === 'available')) {
    const source = await scopedPath(sourceRoot, entry.sourcePath);
    requireCondition(await hashFile(source) === entry.digest, 'ARTIFACT_CHANGED', `Import source changed ${entry.sourcePath}`);
    const target = await scopedPath(targetRoot, entry.targetPath, { mustExist: false });
    await mkdir(dirname(target), { recursive: true });
    try { await copyFile(source, target, 1); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      requireCondition(await hashFile(target) === entry.digest, 'ARTIFACT_CHANGED', `Import target changed ${entry.targetPath}`);
    }
    report.imported.push({ sourcePath: entry.sourcePath, targetPath: entry.targetPath, digest: entry.digest,
      observedAt: utc(), provenanceStatus: 'importObserved', sourceCommit: inventory.sourceCommit });
    store.put('imports', batchId, report);
  }
  report.status = 'completed';
  report.completedAt = utc();
  store.put('imports', batchId, report);
  await writeFile(join(targetRoot, 'IMPORT.json'), JSON.stringify(report, null, 2));
  return report;
}
export function importLegacyState(value, sourceRef) {
  requireCondition(value && typeof value === 'object', 'SCHEMA_INVALID', 'Legacy state must be an object');
  return { ...migrateLegacy(value), legacySourceRef: sourceRef, provenanceStatus: 'imported_unknown', importedAt: utc() };
}
