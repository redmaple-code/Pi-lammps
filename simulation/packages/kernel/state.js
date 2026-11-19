import { DatabaseSync, backup } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { digest, id, utc, requireCondition } from '../contracts/index.js';

const schema = `
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,case_id TEXT NOT NULL,
 state_revision INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,request_digest TEXT NOT NULL,
 status TEXT NOT NULL,payload TEXT NOT NULL,receipt TEXT);
CREATE TABLE IF NOT EXISTS records(kind TEXT NOT NULL,id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,id));
`;
export class SimulationStore {
  constructor(file) {
    mkdirSync(dirname(file), { recursive: true });
    this.file = file;
    this.db = new DatabaseSync(file);
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;');
    const version = this.db.prepare('PRAGMA user_version').get().user_version;
    requireCondition(version <= 2, 'VERSION_INCOMPATIBLE', 'Simulation database is newer than this runtime');
    this.db.exec(schema);
    const checksum = digest(schema);
    const previous = this.db.prepare("SELECT value FROM metadata WHERE key='schema'").get();
    requireCondition(!previous || previous.value === checksum, 'VERSION_INCOMPATIBLE', 'Schema checksum differs');
    this.db.prepare('INSERT OR IGNORE INTO metadata VALUES(?,?)').run('schema', checksum);
    this.db.exec('CREATE TABLE IF NOT EXISTS case_revisions(case_id TEXT NOT NULL,revision_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(case_id,revision_id))');
    this.db.exec("INSERT OR IGNORE INTO case_revisions SELECT id,json_extract(payload,'$.revisionId'),payload FROM cases");
    this.db.exec('PRAGMA user_version=2');
  }
  transaction(action) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = action(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  createCase({ caseId = id('case'), title, revisionId = id('revision'), lock, workflow, scope = 'new', authorization, externalInputs = {} }) {
    requireCondition(title && lock?.digest && workflow?.stages?.length, 'SCHEMA_INVALID', 'Case requires title, lock and workflow');
    const value = { id: caseId, title, revision: 0, revisionId, lock, workflow, scope, authorization,
      createdAt: utc(), status: 'active', externalInputs, stages: workflow.stages.map(stage => ({ ...stage, status: 'pending',
        attempt: 0, consecutiveRevisions: 0, receipts: [], inputRefs: [], outputRefs: [] })), repairs: {}, invalidations: [] };
    this.transaction(() => {
      this.db.prepare('INSERT INTO cases VALUES(?,?,?)').run(caseId, 0, JSON.stringify(value));
      this.db.prepare('INSERT INTO case_revisions VALUES(?,?,?)').run(caseId, revisionId, JSON.stringify(value));
      this.appendEvent(caseId, 0, 'caseCreated', { title, revisionId, lockRef: lock.digest });
    });
    return value;
  }
  getCase(caseId) {
    const row = this.db.prepare('SELECT payload FROM cases WHERE id=?').get(caseId);
    requireCondition(row, 'REFERENCE_MISSING', `Unknown case ${caseId}`);
    return JSON.parse(row.payload);
  }
  cases() { return this.db.prepare('SELECT payload FROM cases ORDER BY rowid DESC').all().map(row => JSON.parse(row.payload)); }
  caseRevision(caseId, revisionId) {
    const row = this.db.prepare('SELECT payload FROM case_revisions WHERE case_id=? AND revision_id=?').get(caseId, revisionId);
    requireCondition(row, 'REFERENCE_MISSING', 'Case revision does not exist');
    return JSON.parse(row.payload);
  }
  forkRevision(caseId, { workflow, lock, authorization, reason, externalInputs }) {
    return this.transaction(() => {
      requireCondition(this.pending(caseId).length === 0, 'RESULT_UNKNOWN', 'Reconcile operations before changing the scientific revision');
      const previous = this.getCase(caseId);
      this.db.prepare('INSERT OR REPLACE INTO case_revisions VALUES(?,?,?)').run(caseId, previous.revisionId, JSON.stringify(previous));
      const next = { ...previous, revision: previous.revision + 1, revisionId: id('revision'), status: 'active', lock, workflow, authorization,
        externalInputs: externalInputs || previous.externalInputs,
        parentRevisionId: previous.revisionId, revisionReason: reason, repairs: {},
        stages: workflow.stages.map(stage => ({ ...stage, status: 'pending', attempt: 0, consecutiveRevisions: 0, receipts: [], inputRefs: [], outputRefs: [] })) };
      this.db.prepare('UPDATE cases SET revision=?,payload=? WHERE id=?').run(next.revision, JSON.stringify(next), caseId);
      this.db.prepare('INSERT INTO case_revisions VALUES(?,?,?)').run(caseId, next.revisionId, JSON.stringify(next));
      this.appendEvent(caseId, next.revision, 'revisionCreated', { previousRevisionId: previous.revisionId, revisionId: next.revisionId, reason });
      return next;
    });
  }
  appendEvent(caseId, revision, kind, payload) {
    const eventId = id('event');
    this.db.prepare('INSERT INTO events(id,case_id,state_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)')
      .run(eventId, caseId, revision, kind, JSON.stringify(payload), utc());
    return eventId;
  }
  dispatch(caseId, command, expectedRevision, reduce) {
    const requestDigest = digest({ caseId, command, expectedRevision });
    return this.transaction(() => {
      const previous = this.db.prepare('SELECT * FROM commands WHERE id=?').get(command.id);
      if (previous) {
        requireCondition(previous.request_digest === requestDigest, 'IDEMPOTENCY_CONFLICT', 'Command ID has different input');
        return JSON.parse(previous.result);
      }
      const state = this.getCase(caseId);
      requireCondition(state.revision === expectedRevision, 'STATE_CONFLICT', 'Case changed', { context: { currentRevision: state.revision } });
      const { events = [], intents = [] } = reduce(state, command);
      state.revision += 1;
      this.db.prepare('UPDATE cases SET revision=?,payload=? WHERE id=?').run(state.revision, JSON.stringify(state), caseId);
      this.db.prepare('INSERT OR REPLACE INTO case_revisions VALUES(?,?,?)').run(caseId, state.revisionId, JSON.stringify(state));
      const eventRefs = events.map(event => this.appendEvent(caseId, state.revision, event.kind, event.payload));
      for (const intent of intents) {
        const requestHash = digest(intent.request);
        this.db.prepare('INSERT INTO operations VALUES(?,?,?,?,?,NULL)').run(intent.operationId, caseId, requestHash,
          'intent', JSON.stringify({ ...intent, createdAt: utc(), revisionId: state.revisionId }));
      }
      const result = { state, eventRefs, operationIds: intents.map(intent => intent.operationId) };
      this.db.prepare('INSERT INTO commands VALUES(?,?,?)').run(command.id, requestDigest, JSON.stringify(result));
      return result;
    });
  }
  operation(operationId) {
    const row = this.db.prepare('SELECT * FROM operations WHERE id=?').get(operationId);
    return row && { ...JSON.parse(row.payload), caseId: row.case_id, status: row.status, receipt: row.receipt ? JSON.parse(row.receipt) : null };
  }
  claim(operationId, owner) {
    return this.transaction(() => {
      const operation = this.operation(operationId);
      requireCondition(operation, 'REFERENCE_MISSING', 'Operation missing');
      requireCondition(operation.status === 'intent', 'RESULT_UNKNOWN', 'Operation was already dispatched. Reconcile before replay.', { sideEffectState: 'unknown' });
      operation.owner = owner;
      operation.dispatchedAt = utc();
      this.db.prepare('UPDATE operations SET status=?,payload=? WHERE id=?').run('dispatched', JSON.stringify(operation), operationId);
      return operation;
    });
  }
  settle(operationId, receipt) {
    requireCondition(['succeeded', 'failed', 'unknown', 'cancelled', 'running', 'queued'].includes(receipt.status), 'SCHEMA_INVALID', 'Invalid operation receipt');
    return this.transaction(() => {
      const operation = this.operation(operationId);
      requireCondition(operation, 'REFERENCE_MISSING', 'Operation missing');
      if (['succeeded', 'failed', 'cancelled'].includes(operation.status)) {
        requireCondition(digest(operation.receipt) === digest(receipt), 'RECEIPT_CONFLICT', 'Terminal receipt already exists');
        return operation;
      }
      this.db.prepare('UPDATE operations SET status=?,receipt=? WHERE id=?').run(receipt.status, JSON.stringify(receipt), operationId);
      return this.operation(operationId);
    });
  }
  pending(caseId) {
    return this.db.prepare("SELECT id FROM operations WHERE case_id=? AND status NOT IN ('succeeded','failed','cancelled')")
      .all(caseId).map(row => this.operation(row.id));
  }
  operations(caseId) {
    return this.db.prepare('SELECT id FROM operations WHERE case_id=? ORDER BY rowid').all(caseId).map(row => this.operation(row.id));
  }
  put(kind, key, payload, expectedVersion) {
    const write = () => {
      const row = this.db.prepare('SELECT version,payload FROM records WHERE kind=? AND id=?').get(kind, key);
      if (expectedVersion !== undefined) requireCondition((row?.version || 0) === expectedVersion, 'STATE_CONFLICT', `${kind} changed`);
      const version = (row?.version || 0) + 1;
      this.db.prepare('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET version=excluded.version,payload=excluded.payload')
        .run(kind, key, version, JSON.stringify(payload));
      return { ...payload, recordVersion: version };
    };
    return this.transaction(write);
  }
  get(kind, key) {
    const row = this.db.prepare('SELECT version,payload FROM records WHERE kind=? AND id=?').get(kind, key);
    return row && { ...JSON.parse(row.payload), recordVersion: row.version };
  }
  list(kind) { return this.db.prepare('SELECT id FROM records WHERE kind=? ORDER BY rowid').all(kind).map(row => this.get(kind, row.id)); }
  events(caseId, after = 0) {
    return this.db.prepare('SELECT * FROM events WHERE case_id=? AND seq>? ORDER BY seq').all(caseId, after)
      .map(row => ({ ...row, payload: JSON.parse(row.payload) }));
  }
  async backup(file) { await backup(this.db, file); return { file, createdAt: utc() }; }
  health() { return this.db.prepare('PRAGMA integrity_check').get(); }
  close() { this.db.close(); }
}
