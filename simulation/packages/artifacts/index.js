import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { mkdir, open, readFile, stat, realpath, rename, copyFile, rm } from 'node:fs/promises';
import { resolve, relative, isAbsolute, dirname, join } from 'node:path';
import { digest, id, utc, requireCondition } from '../contracts/index.js';

export async function hashFile(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return `sha256:${hash.digest('hex')}`;
}
export async function scopedPath(root, path, { mustExist = true } = {}) {
  const base = await realpath(root);
  const candidate = resolve(base, path);
  let target;
  if (mustExist) target = await realpath(candidate);
  else {
    let ancestor = candidate;
    while (true) {
      try { target = resolve(await realpath(ancestor), relative(ancestor, candidate)); break; }
      catch (error) { if (error.code !== 'ENOENT' || dirname(ancestor) === ancestor) throw error; ancestor = dirname(ancestor); }
    }
  }
  const part = relative(base, target);
  requireCondition(part !== '..' && !part.startsWith('..\\') && !part.startsWith('../') && !isAbsolute(part), 'OUTSIDE_WORKSPACE', 'Path escapes root');
  return target;
}
export class ArtifactStore {
  constructor(root, state) { this.root = resolve(root); this.state = state; }
  async publish(path, metadata = {}) {
    const sourceHash = await hashFile(path);
    const directory = join(this.root, 'objects', sourceHash.slice(7, 9));
    await mkdir(directory, { recursive: true });
    const target = join(directory, sourceHash.slice(7));
    const temporary = `${target}.${id('staged')}`;
    await copyFile(path, temporary);
    try {
      requireCondition(await hashFile(temporary) === sourceHash, 'ARTIFACT_CHANGED', 'Source changed during publication');
      const handle = await open(temporary, 'r+');
      try { await handle.sync(); } finally { await handle.close(); }
      try { await rename(temporary, target); }
      catch (error) {
        if (!['EEXIST', 'EPERM'].includes(error.code)) throw error;
        requireCondition(await hashFile(target) === sourceHash, 'ARTIFACT_CHANGED', 'Existing content is corrupt');
      }
    } finally { await rm(temporary, { force: true }); }
    const bytes = (await stat(target)).size;
    const ref = { artifactId: id('artifact'), digest: sourceHash, byteLength: bytes, locator: target,
      type: metadata.type || 'file', status: 'available', createdAt: utc(), metadata, dependencies: metadata.dependencies || [] };
    this.state?.put('artifacts', ref.artifactId, ref, 0);
    return ref;
  }
  async text(text, metadata = {}) {
    await mkdir(join(this.root, 'staging'), { recursive: true });
    const path = join(this.root, 'staging', id('text'));
    const handle = await open(path, 'wx');
    try { await handle.writeFile(text, 'utf8'); await handle.sync(); } finally { await handle.close(); }
    try { return await this.publish(path, metadata); } finally { await rm(path, { force: true }); }
  }
  async json(value, metadata = {}) { return this.text(JSON.stringify(value, null, 2), { ...metadata, type: metadata.type || 'json' }); }
  async verify(ref) {
    const actual = await hashFile(ref.locator);
    requireCondition(actual === ref.digest && (await stat(ref.locator)).size === ref.byteLength, 'ARTIFACT_CHANGED', `Changed artifact ${ref.artifactId}`);
    return { artifactId: ref.artifactId, digest: actual, verifiedAt: utc() };
  }
  async read(ref) { await this.verify(ref); return readFile(ref.locator); }
  async closure(entries) {
    const sorted = [];
    for (const { path, ref } of entries) {
      requireCondition(!isAbsolute(path) && !path.split(/[\\/]/).includes('..'), 'OUTSIDE_WORKSPACE', 'Closure paths must be relative');
      await this.verify(ref);
      sorted.push({ path: path.replaceAll('\\', '/'), ref });
    }
    sorted.sort((a, b) => a.path.localeCompare(b.path, 'en'));
    requireCondition(new Set(sorted.map(item => item.path)).size === sorted.length, 'SCHEMA_INVALID', 'Duplicate closure path');
    return { algorithm: 'closure-json-v1', digest: digest(sorted.map(({ path, ref }) => ({ path, digest: ref.digest, byteLength: ref.byteLength }))), entries: sorted };
  }
  async materialize(closure, targetRoot) {
    await mkdir(targetRoot, { recursive: true });
    for (const { path, ref } of closure.entries) {
      await this.verify(ref);
      const destination = await scopedPath(targetRoot, path, { mustExist: false });
      await mkdir(dirname(destination), { recursive: true });
      const handle = await open(destination, 'wx');
      try { await handle.writeFile(await readFile(ref.locator)); } finally { await handle.close(); }
    }
  }
}
