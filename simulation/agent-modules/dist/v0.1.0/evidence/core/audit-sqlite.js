import { DatabaseSync } from 'node:sqlite';
import { digestJson, requireThat } from './index.js';

// Storage path and directory ownership are supplied and secured by the host.
export class SqliteAudit {
  constructor(path) {
    requireThat(typeof path === 'string' && path.length > 0, 'CONFIG_INVALID', 'Audit database path required');
    this.db = new DatabaseSync(path);
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000; CREATE TABLE IF NOT EXISTS module_audit(seq INTEGER PRIMARY KEY, event TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL)');
  }
  append(event) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      requireThat(this.verify().ok, 'AUDIT_CORRUPT', 'Existing audit chain is invalid');
      const last = this.db.prepare('SELECT seq,digest FROM module_audit ORDER BY seq DESC LIMIT 1').get();
      const seq = (last?.seq || 0) + 1, previous = last?.digest || 'genesis';
      const record = { ...event, timestamp: new Date().toISOString() };
      const digest = digestJson({ seq, previous, record });
      this.db.prepare('INSERT INTO module_audit VALUES(?,?,?,?)').run(seq, JSON.stringify(record), previous, digest);
      this.db.exec('COMMIT');
      return { seq, digest };
    } catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  verify() {
    let previous = 'genesis', seq = 0;
    for (const row of this.db.prepare('SELECT * FROM module_audit ORDER BY seq').iterate()) {
      seq++;
      let record; try { record = JSON.parse(row.event); } catch { return { ok: false, seq }; }
      if (row.seq !== seq || row.previous !== previous || row.digest !== digestJson({ seq, previous, record })) return { ok: false, seq };
      previous = row.digest;
    }
    return { ok: true, count: seq, head: previous };
  }
  close() { this.db.close(); }
}
