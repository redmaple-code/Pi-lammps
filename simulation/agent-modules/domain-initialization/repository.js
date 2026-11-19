import { mkdir, open, readFile, readdir, rename, unlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { requireThat } from '../core/index.js';

function validId(id) {
  requireThat(typeof id === 'string' && /^library-[a-f0-9-]{36}$/.test(id), 'SCHEMA_INVALID', 'Invalid library identity');
  return id;
}

// The service depends on read/list/save. Other storage adapters can implement it.
export class MemoryLibraryRepository {
  constructor() { this.documents = new Map(); }
  async read(id) { return structuredClone(this.documents.get(validId(id)) || null); }
  async list() { return structuredClone([...this.documents.values()]); }
  async save(document, expectedRevision) {
    validId(document.id);
    requireThat((this.documents.get(document.id)?.revision || 0) === expectedRevision,
      'REVISION_CONFLICT', 'Library changed; reload before applying this operation');
    requireThat(document.revision === expectedRevision + 1, 'SCHEMA_INVALID', 'Revision must advance once');
    this.documents.set(document.id, structuredClone(document));
  }
}

export class FileLibraryRepository {
  constructor(directory) { this.directory = resolve(directory); }
  path(id) { return join(this.directory, `${validId(id)}.json`); }
  async read(id) {
    try { return JSON.parse(await readFile(this.path(id), 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  async list() {
    await mkdir(this.directory, { recursive: true });
    const names = (await readdir(this.directory)).filter(name => /^library-[a-f0-9-]{36}\.json$/.test(name));
    return Promise.all(names.map(name => this.read(name.slice(0, -5))));
  }
  async save(document, expectedRevision) {
    await mkdir(this.directory, { recursive: true });
    const destination = this.path(document.id), lockPath = `${destination}.lock`;
    let lock;
    try { lock = await open(lockPath, 'wx', 0o600); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      let owner;
      try { owner = JSON.parse(await readFile(lockPath, 'utf8')); } catch {}
      let alive = true;
      if (Number.isInteger(owner?.pid)) {
        try { process.kill(owner.pid, 0); } catch (failure) { if (failure.code === 'ESRCH') alive = false; }
      }
      requireThat(false, alive ? 'REVISION_CONFLICT' : 'LOCK_RECOVERY_REQUIRED',
        alive ? 'Library is being updated; reload before retrying' : 'A stopped writer left a lock; verify the local repository before clearing it');
    }
    const temporary = `${destination}.${randomUUID()}.tmp`;
    try {
      await lock.writeFile(JSON.stringify({ pid: process.pid }));
      const previous = await this.read(document.id);
      requireThat((previous?.revision || 0) === expectedRevision, 'REVISION_CONFLICT', 'Library changed; reload first');
      requireThat(document.revision === expectedRevision + 1, 'SCHEMA_INVALID', 'Revision must advance once');
      const file = await open(temporary, 'wx', 0o600);
      try { await file.writeFile(JSON.stringify(document, null, 2)); await file.sync(); }
      finally { await file.close(); }
      await rename(temporary, destination);
    } finally {
      try { await unlink(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      await lock.close();
      await unlink(lockPath);
    }
  }
}
