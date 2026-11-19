import { readFile, writeFile, mkdir, realpath } from 'node:fs/promises';
import { join, resolve, basename } from 'node:path';
import { randomUUID } from 'node:crypto';
import { requireThat } from '../core/index.js';
import { checkLibraryContent, checkMaterialPath } from './privacy.js';
import { readLibraryMaterial } from './materials.js';

export async function runDomainInitializationCli({ host, directory }, { action = 'list', libraryId, inputFile, file, title, objective, sourceType, query }) {
  const actor = { actorId: 'domain-library-cli', actorInstanceId: randomUUID(), kind: 'service' };
  const invoke = (name, input) => host.call('domain-initialization', name, input, { actor, requestId: randomUUID() });
  let input = {};
  if (inputFile) {
    checkMaterialPath(resolve(inputFile));
    checkMaterialPath(await realpath(resolve(inputFile)));
    const raw = await readFile(inputFile, 'utf8');
    checkLibraryContent(raw);
    input = JSON.parse(raw);
  }
  if (action === 'list') return invoke('list', {});
  if (action === 'new') return invoke('create', { ...input, ...(title ? { title } : {}), ...(objective ? { objective } : {}), operationId: input.operationId || randomUUID() });
  requireThat(libraryId, 'SCHEMA_INVALID', 'Specify the library ID');
  if (action === 'source') {
    if (file) input = { ...input, ...await readLibraryMaterial(file), title: title || basename(file), sourceType: sourceType || 'other' };
    return invoke('source', { ...input, libraryId, operationId: input.operationId || randomUUID() });
  }
  if (action === 'propose') return invoke('propose', { ...input, libraryId, operationId: input.operationId || randomUUID() });
  if (action === 'query') return invoke('query', { ...input, libraryId, ...(query === undefined ? {} : { query }) });
  if (action === 'export') {
    const snapshot = await invoke('snapshot', { libraryId });
    const exports = join(directory, 'exports');
    await mkdir(exports, { recursive: true });
    const path = join(exports, `${libraryId}-${snapshot.revision}-${randomUUID()}.json`);
    await writeFile(path, JSON.stringify(snapshot, null, 2), { flag: 'wx', mode: 0o600 });
    return { libraryId, path, snapshotDigest: snapshot.snapshotDigest };
  }
  requireThat(['status', 'guide', 'research'].includes(action), 'USER_DECISION_REQUIRED', 'Use /domain-init in Pi for direct user answers and adoption decisions');
  return invoke(action, { libraryId });
}
