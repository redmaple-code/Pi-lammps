-- UKB schema v1. Authoritative store; every table below is written only by ukb.core.
PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ===== registries =====
CREATE TABLE reg_setting (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE reg_type (
  type_id         TEXT PRIMARY KEY,
  label           TEXT NOT NULL,
  description     TEXT NOT NULL,
  current_version INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reg_type_version (
  type_id        TEXT NOT NULL REFERENCES reg_type(type_id),
  version        INTEGER NOT NULL,
  schema_json    TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (type_id, version)
);

CREATE TABLE reg_facet (
  facet_id           TEXT PRIMARY KEY,
  label              TEXT NOT NULL,
  description        TEXT NOT NULL,
  multi              INTEGER NOT NULL DEFAULT 1 CHECK (multi IN (0,1)),
  governing          INTEGER NOT NULL DEFAULT 0 CHECK (governing IN (0,1)),
  scope_budget_chars INTEGER,
  ord                INTEGER NOT NULL DEFAULT 0,
  status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE tag (
  tag_id        INTEGER PRIMARY KEY,
  facet_id      TEXT NOT NULL REFERENCES reg_facet(facet_id),
  slug          TEXT NOT NULL,
  label         TEXT NOT NULL,
  description   TEXT NOT NULL,
  parent_tag_id INTEGER REFERENCES tag(tag_id),
  status        TEXT NOT NULL DEFAULT 'candidate'
                CHECK (status IN ('candidate','active','deprecated','merged')),
  merged_into   INTEGER REFERENCES tag(tag_id),
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE (facet_id, slug),
  CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
  CHECK (parent_tag_id IS NULL OR parent_tag_id <> tag_id)
);

CREATE TABLE tag_alias (
  alias_norm TEXT NOT NULL,
  tag_id     INTEGER NOT NULL REFERENCES tag(tag_id),
  lang       TEXT,
  PRIMARY KEY (alias_norm, tag_id)
);

CREATE TABLE reg_condition_key (
  key_id      TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  value_type  TEXT NOT NULL CHECK (value_type IN ('number','integer','text','enum','bool')),
  unit        TEXT,
  enum_json   TEXT,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reg_relation (
  rel_id        TEXT PRIMARY KEY,
  label         TEXT NOT NULL,
  inverse_label TEXT NOT NULL,
  symmetric     INTEGER NOT NULL DEFAULT 0 CHECK (symmetric IN (0,1)),
  acyclic       INTEGER NOT NULL DEFAULT 0 CHECK (acyclic IN (0,1)),
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reg_source_type (
  source_type_id TEXT PRIMARY KEY,
  label          TEXT NOT NULL,
  default_grade  INTEGER NOT NULL DEFAULT 0 CHECK (default_grade BETWEEN 0 AND 9),
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE consumer_profile (
  profile_id  TEXT PRIMARY KEY,
  spec_json   TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE risk_rule (
  rule_id     TEXT PRIMARY KEY,
  match_kind  TEXT NOT NULL CHECK (match_kind IN ('keyword','regex','tag','type')),
  pattern     TEXT NOT NULL,
  action      TEXT NOT NULL CHECK (action IN ('flag','quarantine')),
  note        TEXT,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reg_artifact_role (
  role_id          TEXT PRIMARY KEY,
  label            TEXT NOT NULL,
  description      TEXT NOT NULL,
  default_reusable INTEGER NOT NULL DEFAULT 0 CHECK (default_reusable IN (0,1)),
  formats_json     TEXT,
  status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reg_file_format (
  format_id     TEXT PRIMARY KEY,
  label         TEXT NOT NULL,
  patterns_json TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('text','pdf','binary')),
  magic_hex     TEXT,
  max_bytes     INTEGER,
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reg_file_deny (
  pattern TEXT PRIMARY KEY,
  reason  TEXT NOT NULL,
  status  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated'))
);

CREATE TABLE reviewer (
  reviewer_id TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('admin','llm')),
  token_salt  TEXT,
  token_hash  TEXT,
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  created_at  TEXT NOT NULL,
  CHECK ((kind = 'llm') = (reviewer_id LIKE 'llm:%')),
  CHECK (kind = 'llm' OR (token_salt IS NOT NULL AND token_hash IS NOT NULL)),
  CHECK (kind = 'admin' OR (token_salt IS NULL AND token_hash IS NULL))
);

-- ===== entries and content =====
CREATE TABLE entry (
  entry_id    TEXT PRIMARY KEY,
  type_id     TEXT NOT NULL REFERENCES reg_type(type_id),
  status      TEXT NOT NULL CHECK (status IN
              ('candidate','trial','active','deprecated','superseded','revoked','rejected','quarantined')),
  head_rev    TEXT,
  status_note TEXT,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE entry_revision (
  rev_id        TEXT PRIMARY KEY,
  entry_id      TEXT NOT NULL REFERENCES entry(entry_id),
  rev_no        INTEGER NOT NULL,
  type_id       TEXT NOT NULL,
  type_version  INTEGER NOT NULL,
  title         TEXT NOT NULL,
  fields_json   TEXT NOT NULL,
  char_count    INTEGER NOT NULL,
  manifest_hash TEXT,
  body_hash     TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  parent_rev    TEXT REFERENCES entry_revision(rev_id),
  author        TEXT NOT NULL,
  change_note   TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (entry_id, rev_no),
  FOREIGN KEY (type_id, type_version) REFERENCES reg_type_version(type_id, version)
);

CREATE TABLE rev_approval (
  rev_id      TEXT PRIMARY KEY REFERENCES entry_revision(rev_id),
  task_id     TEXT NOT NULL REFERENCES review_task(task_id),
  approved_at TEXT NOT NULL
);

CREATE TABLE entry_condition (
  rev_id     TEXT NOT NULL REFERENCES entry_revision(rev_id),
  key_id     TEXT NOT NULL REFERENCES reg_condition_key(key_id),
  op         TEXT NOT NULL CHECK (op IN ('eq','ne','lt','le','gt','ge','in','range','exists')),
  value_json TEXT NOT NULL,
  unit       TEXT,
  note       TEXT,
  PRIMARY KEY (rev_id, key_id, op)
);

CREATE TABLE entry_tag (
  entry_id   TEXT NOT NULL REFERENCES entry(entry_id),
  tag_id     INTEGER NOT NULL REFERENCES tag(tag_id),
  state      TEXT NOT NULL CHECK (state IN ('suggested','confirmed','rejected')),
  origin     TEXT NOT NULL CHECK (origin IN ('submitter','prereview','plugin','import','human')),
  reason     TEXT,
  decided_by TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (entry_id, tag_id)
);

CREATE TABLE blob (
  sha256     TEXT PRIMARY KEY,
  size       INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE source (
  source_id        TEXT PRIMARY KEY,
  source_type_id   TEXT NOT NULL REFERENCES reg_source_type(source_type_id),
  title            TEXT NOT NULL,
  locator          TEXT NOT NULL,
  grade            INTEGER NOT NULL CHECK (grade BETWEEN 0 AND 9),
  independence_key TEXT,
  added_at         TEXT NOT NULL,
  UNIQUE (source_type_id, locator)
);

CREATE TABLE entry_source (
  rev_id         TEXT NOT NULL REFERENCES entry_revision(rev_id),
  source_id      TEXT NOT NULL REFERENCES source(source_id),
  role           TEXT NOT NULL CHECK (role IN ('supports','contradicts','derived_from','example')),
  locator_detail TEXT,
  excerpt        TEXT,
  PRIMARY KEY (rev_id, source_id, role)
);

-- ===== document-tier entries =====
CREATE TABLE doc_file (
  rev_id       TEXT NOT NULL REFERENCES entry_revision(rev_id),
  path         TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 255 AND substr(path,1,1) <> '/'
                                    AND instr(path,'..') = 0 AND instr(path, char(92)) = 0
                                    AND instr(path,':') = 0 AND instr(path,'//') = 0),
  role_id      TEXT NOT NULL REFERENCES reg_artifact_role(role_id),
  format_id    TEXT NOT NULL REFERENCES reg_file_format(format_id),
  description  TEXT NOT NULL,
  sha256       TEXT NOT NULL REFERENCES blob(sha256),
  size         INTEGER NOT NULL CHECK (size >= 0),
  text_sha256  TEXT REFERENCES blob(sha256),
  char_count   INTEGER,
  line_count   INTEGER,
  page_count   INTEGER,
  reusable     INTEGER NOT NULL CHECK (reusable IN (0,1)),
  license_note TEXT,
  PRIMARY KEY (rev_id, path)
);

CREATE TABLE doc_section (
  rev_id     TEXT NOT NULL,
  path       TEXT NOT NULL,
  section_id TEXT NOT NULL,
  title      TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  start_char INTEGER NOT NULL CHECK (start_char >= 0),
  end_char   INTEGER NOT NULL,
  PRIMARY KEY (rev_id, path, section_id),
  FOREIGN KEY (rev_id, path) REFERENCES doc_file(rev_id, path),
  CHECK (end_char > start_char)
);

CREATE TABLE checkout_log (
  checkout_id INTEGER PRIMARY KEY AUTOINCREMENT,
  rev_id      TEXT NOT NULL REFERENCES entry_revision(rev_id),
  paths_json  TEXT NOT NULL,
  dest        TEXT NOT NULL,
  actor       TEXT NOT NULL,
  task_ref    TEXT,
  ts          TEXT NOT NULL
);

-- ===== intake and review =====
CREATE TABLE intake (
  intake_id       TEXT PRIMARY KEY,
  received_at     TEXT NOT NULL,
  channel         TEXT NOT NULL CHECK (channel IN ('mcp','cli','webui','import','api')),
  submitter       TEXT NOT NULL,
  mode            TEXT NOT NULL,
  context_json    TEXT,
  raw_json        TEXT NOT NULL,
  raw_hash        TEXT NOT NULL,
  state           TEXT NOT NULL CHECK (state IN
                  ('invalid','queued','committed','rejected','changes_requested','quarantined')),
  target_entry_id TEXT REFERENCES entry(entry_id),
  result_entry_id TEXT REFERENCES entry(entry_id),
  issues_json     TEXT
);

CREATE TABLE review_task (
  task_id        TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN
                 ('new','revise','reinforce','status_change','tag_confirm','registry_change')),
  intake_id      TEXT REFERENCES intake(intake_id),
  entry_id       TEXT REFERENCES entry(entry_id),
  rev_id         TEXT REFERENCES entry_revision(rev_id),
  submitter      TEXT NOT NULL,
  payload_json   TEXT,
  bound_hash     TEXT NOT NULL,
  prereview_required INTEGER NOT NULL CHECK (prereview_required IN (0,1)),
  risk_hits_json TEXT,
  exact_dup_of   TEXT REFERENCES entry_revision(rev_id),
  state          TEXT NOT NULL CHECK (state IN ('open','approved','rejected','changes_requested')),
  opened_at      TEXT NOT NULL,
  closed_at      TEXT
);

CREATE TABLE review_decision (
  decision_id        TEXT PRIMARY KEY,
  task_id            TEXT NOT NULL REFERENCES review_task(task_id),
  reviewer_id        TEXT NOT NULL REFERENCES reviewer(reviewer_id),
  reviewer_kind      TEXT NOT NULL CHECK (reviewer_kind IN ('admin','llm')),
  channel            TEXT NOT NULL,
  decision           TEXT NOT NULL CHECK (decision IN
                     ('approve','approve_as_trial','request_changes','reject',
                      'correct','reinforce_into','prereview')),
  rationale          TEXT NOT NULL,
  opinion_json       TEXT,
  adopted_json       TEXT,
  tag_decisions_json TEXT,
  bound_hash         TEXT NOT NULL,
  decided_at         TEXT NOT NULL,
  CHECK ((decision = 'prereview') = (reviewer_kind = 'llm'))
);

CREATE TABLE reinforcement (
  reinforce_id TEXT PRIMARY KEY,
  entry_id     TEXT NOT NULL REFERENCES entry(entry_id),
  source_id    TEXT NOT NULL REFERENCES source(source_id),
  task_id      TEXT NOT NULL REFERENCES review_task(task_id),
  note         TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (entry_id, source_id)
);

CREATE TABLE relation (
  src_entry  TEXT NOT NULL REFERENCES entry(entry_id),
  rel_id     TEXT NOT NULL REFERENCES reg_relation(rel_id),
  dst_entry  TEXT NOT NULL REFERENCES entry(entry_id),
  note       TEXT,
  task_id    TEXT REFERENCES review_task(task_id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (src_entry, rel_id, dst_entry),
  CHECK (src_entry <> dst_entry)
);

-- ===== audit and feedback =====
CREATE TABLE audit_log (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  object_kind TEXT NOT NULL,
  object_id   TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL UNIQUE
);

CREATE TABLE usage_log (
  usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id TEXT NOT NULL REFERENCES entry(entry_id),
  rev_no   INTEGER,
  consumer TEXT NOT NULL,
  task_ref TEXT,
  outcome  TEXT NOT NULL CHECK (outcome IN
           ('helpful','neutral','misleading','not_applicable','reused_ok','reused_failed')),
  note     TEXT,
  ts       TEXT NOT NULL
);

CREATE INDEX ix_entry_type_status ON entry(type_id, status);
CREATE INDEX ix_rev_entry        ON entry_revision(entry_id);
CREATE INDEX ix_rev_hash         ON entry_revision(content_hash);
CREATE INDEX ix_rev_body         ON entry_revision(body_hash);
CREATE INDEX ix_tag_facet        ON tag(facet_id, status);
CREATE INDEX ix_tag_parent       ON tag(parent_tag_id);
CREATE INDEX ix_alias_tag        ON tag_alias(tag_id);
CREATE INDEX ix_entry_tag_tag    ON entry_tag(tag_id, state);
CREATE INDEX ix_es_source        ON entry_source(source_id);
CREATE INDEX ix_rel_dst          ON relation(dst_entry, rel_id);
CREATE INDEX ix_intake_state     ON intake(state, received_at);
CREATE INDEX ix_intake_submitter ON intake(submitter, received_at);
CREATE INDEX ix_task_state       ON review_task(state, opened_at);
CREATE INDEX ix_task_entry       ON review_task(entry_id, state);
CREATE INDEX ix_decision_task    ON review_decision(task_id);
CREATE INDEX ix_usage_entry      ON usage_log(entry_id, outcome);
CREATE INDEX ix_doc_file_sha     ON doc_file(sha256);
CREATE INDEX ix_checkout_rev     ON checkout_log(rev_id);

CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER rev_immutable BEFORE UPDATE ON entry_revision
BEGIN SELECT RAISE(ABORT, 'entry_revision is immutable'); END;
CREATE TRIGGER rev_no_delete BEFORE DELETE ON entry_revision
BEGIN SELECT RAISE(ABORT, 'entry_revision is immutable'); END;
CREATE TRIGGER doc_file_immutable BEFORE UPDATE ON doc_file
BEGIN SELECT RAISE(ABORT, 'doc_file is immutable'); END;
CREATE TRIGGER doc_file_no_delete BEFORE DELETE ON doc_file
BEGIN SELECT RAISE(ABORT, 'doc_file is immutable'); END;
CREATE TRIGGER doc_section_immutable BEFORE UPDATE ON doc_section
BEGIN SELECT RAISE(ABORT, 'doc_section is immutable'); END;
CREATE TRIGGER entry_source_immutable BEFORE UPDATE ON entry_source
BEGIN SELECT RAISE(ABORT, 'entry_source is immutable'); END;
CREATE TRIGGER entry_condition_immutable BEFORE UPDATE ON entry_condition
BEGIN SELECT RAISE(ABORT, 'entry_condition is immutable'); END;
CREATE TRIGGER decision_immutable BEFORE UPDATE ON review_decision
BEGIN SELECT RAISE(ABORT, 'review_decision is immutable'); END;
CREATE TRIGGER intake_raw_immutable BEFORE UPDATE OF raw_json, raw_hash ON intake
BEGIN SELECT RAISE(ABORT, 'intake raw payload is immutable'); END;

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2');
