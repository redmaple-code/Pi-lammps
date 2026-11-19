import { DatabaseSync } from 'node:sqlite';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { digest, digestBytes, requireCondition } from '../contracts/index.js';
import { hashFile } from '../artifacts/index.js';

export class LegacyIndexBackend {
  constructor(file) {
    this.file = file;
    this.db = new DatabaseSync(file, { readOnly: true });
    requireCondition(this.db.prepare('PRAGMA integrity_check').get().integrity_check === 'ok', 'SOURCE_CORRUPT', 'Legacy index integrity failed');
  }
  summary() {
    return { chunks: this.db.prepare('SELECT count(*) AS n FROM documents').get().n,
      sources: this.db.prepare('SELECT count(DISTINCT path) AS n FROM documents').get().n,
      groups: this.db.prepare('SELECT source_type,count(DISTINCT path) AS sources,count(*) AS chunks FROM documents GROUP BY source_type').all() };
  }
  search(query, { limit = 12, sourceType } = {}) {
    requireCondition(query.length > 0 && query.length <= 500, 'SCHEMA_INVALID', 'Query length invalid');
    limit = Math.min(50, Math.max(1, limit));
    const filter = sourceType ? 'AND source_type=?' : '';
    const args = sourceType ? [`%${query}%`, `%${query}%`, sourceType, limit] : [`%${query}%`, `%${query}%`, limit];
    return this.db.prepare(`SELECT path,title,source_type,source_tier,material_system,potential_family,stage,
      min(chunk_index) AS first_chunk,count(*) AS matching_chunks FROM documents WHERE (content LIKE ? OR path LIKE ?) ${filter}
      GROUP BY path ORDER BY matching_chunks DESC,path LIMIT ?`).all(...args)
      .map(item => ({ ...item, sourceId: digest(item.path), originGroupId: item.path, provenance: 'indexedSnapshot',
        whyRanked: ['legacy-content-match', 'distinct-original-path'], originalAvailable: false }));
  }
  async readSnapshot(path, { outputRoot, evidence, actor }) {
    const rows = this.db.prepare('SELECT id,chunk_index,content,char_count,sha1 FROM documents WHERE path=? ORDER BY chunk_index').all(path);
    requireCondition(rows.length > 0, 'REFERENCE_MISSING', 'Indexed source missing');
    const materialized = rows.map(row => `--- indexed chunk ${row.chunk_index}, record ${row.id} ---\n${row.content}`).join('\n\n');
    await mkdir(outputRoot, { recursive: true });
    const output = join(outputRoot, `${digest(path).slice(7)}.txt`);
    const previous = await readFile(output).catch(error => error.code === 'ENOENT' ? null : Promise.reject(error));
    if (!previous) await writeFile(output, materialized, { flag: 'wx' });
    else requireCondition(digestBytes(previous) === digestBytes(materialized), 'ARTIFACT_CHANGED', 'Indexed snapshot projection changed');
    const sourceKind = path.includes('/cases/') ? 'case' : path.includes('/rules/') ? 'correction' : 'memory';
    const receipt = await evidence.read({ sourceId: digest(path), locator: output, digest: await hashFile(output), sourceKind,
      originGroupId: path, applicability: { originalPath: path, indexedNormalizedSha1: rows[0].sha1, indexDigest: await hashFile(this.file),
        representation: 'indexed-chunks', originalBytesVerified: false } }, { representation: 'section' }, actor);
    return { receipt, originalPath: path, chunkCount: rows.length, originalBytesVerified: false, locator: output };
  }
  close() { this.db.close(); }
}
