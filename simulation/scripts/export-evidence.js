import { parseArgs } from 'node:util';
import { execFileSync } from 'node:child_process';
import { readdir, mkdir, writeFile, copyFile, stat } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { openApplication } from '../packages/application/index.js';
import { hashFile } from '../packages/artifacts/index.js';
import { digest, utc, requireCondition } from '../packages/contracts/index.js';
import { simulationRoot } from './build-packs.js';

const { values } = parseArgs({ options: {
  output: { type: 'string' }, workspace: { type: 'string' }, state: { type: 'string' }
} });
requireCondition(values.output, 'SCHEMA_INVALID', 'Supply --output with a new snapshot directory');
const workspace = resolve(values.workspace || simulationRoot), output = resolve(values.output);
await mkdir(output, { recursive: false });
const app = await openApplication({ workspace, stateDirectory: values.state });
try {
  const cases = app.store.cases();
  const pending = cases.flatMap(item => app.store.pending(item.id));
  requireCondition(!pending.length, 'RESULT_UNKNOWN', 'Finish or reconcile pending operations before exporting a joint snapshot');
  await app.store.backup(join(output, 'simulation.sqlite'));
  await app.knowledge.backup(join(output, 'knowledge.sqlite'));
  for (const name of ['simulation.sqlite', 'knowledge.sqlite']) {
    const database = new DatabaseSync(join(output, name), { readOnly: true });
    try { requireCondition(database.prepare('PRAGMA integrity_check').get().integrity_check === 'ok', 'BACKUP_FAILED', `${name} failed integrity check`); }
    finally { database.close(); }
  }
  const writeJson = (name, value) => writeFile(join(output, name), JSON.stringify(value, null, 2), { flag: 'wx' });
  const records = app.store.db.prepare('SELECT kind,id,version,payload FROM records ORDER BY kind,id').all()
    .map(row => ({ ...row, payload: JSON.parse(row.payload) }));
  await writeJson('records.json', records);
  await writeJson('cases.json', cases.map(state => ({ state, events: app.store.events(state.id), operations: app.store.operations(state.id) })));
  const sources = [];
  const ignored = new Set(['node_modules', 'work', '__pycache__', '.local', '.git']);
  async function walk(directory) {
    for (const item of await readdir(directory, { withFileTypes: true })) {
      if (ignored.has(item.name)) continue;
      const path = join(directory, item.name);
      if (item.isDirectory()) await walk(path);
      else if (item.isFile() && !item.name.endsWith('.log')) sources.push({ path, digest: await hashFile(path), byteLength: (await stat(path)).size });
    }
  }
  await walk(workspace);
  const repository = join(simulationRoot, '..', 'redmaple-agent');
  const baselineCommit = execFileSync('git', ['-C', repository, 'rev-parse', 'HEAD'], { encoding: 'utf8', windowsHide: true }).trim();
  const patch = execFileSync('git', ['-C', repository, 'diff', '--binary', 'HEAD'], { encoding: 'utf8', windowsHide: true });
  await writeFile(join(output, 'redmaple.patch'), patch, { flag: 'wx' });
  const sourceFiles = sources.map(item => ({ ...item, path: relative(workspace, item.path).replaceAll('\\', '/') })).sort((a, b) => a.path.localeCompare(b.path));
  await writeJson('source-manifest.json', { baselineCommit, sourceFiles, sourceFingerprint: digest(sourceFiles) });
  const artifacts = app.store.list('artifacts');
  const unique = new Map(artifacts.map(ref => [ref.locator, ref]));
  for (const ref of unique.values()) await app.artifacts.verify(ref);
  await writeJson('artifact-manifest.json', artifacts);
  for (const item of await readdir(simulationRoot)) {
    if (item.endsWith('.log') || ['atomsk-validation.json', 'final-audit-inspection.json'].includes(item)) {
      await copyFile(join(simulationRoot, item), join(output, item), 1);
    }
  }
  const exportedFiles = [];
  for (const item of await readdir(output)) exportedFiles.push({ path: item, digest: await hashFile(join(output, item)) });
  const manifest = { schemaVersion: 'delivery/1', createdAt: utc(), workspace, stateDirectory: app.stateDirectory,
    baselineCommit, sourceFingerprint: digest(sourceFiles), sourceFileCount: sourceFiles.length,
    caseCount: cases.length, artifactCount: artifacts.length, distinctArtifactObjects: unique.size,
    databaseIntegrity: 'ok', pendingOperations: 0, exportedFiles,
    externalArtifactRoot: app.artifacts.root,
    restoreScope: 'Original workspace path. Preserve work/artifacts, work/cases and work/jobs alongside this database snapshot.' };
  await writeJson('manifest.json', manifest);
  console.log(JSON.stringify({ output, ...manifest }, null, 2));
} finally { app.close(); }
