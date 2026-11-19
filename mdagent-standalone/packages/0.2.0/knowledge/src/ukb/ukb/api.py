"""Facade used by CLI, MCP server and web console. Every write runs in one immediate transaction."""
import os

from .core import audit, contribute, db, maintenance, navigate, registry, review
from .core import intake as _intake
from .core.blobs import BlobStore
from .core.errors import UKBError

DEFAULT_PACK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "seed")


class UKB:
    def __init__(self, root, journal_mode="DELETE", pdf_extractor="auto"):
        self.root = os.path.abspath(root)
        self.db_path = os.path.join(self.root, "ukb.sqlite")
        self.blobs = BlobStore(os.path.join(self.root, "blobs"))
        self.conn = db.connect(self.db_path, journal_mode)
        self._pdf_mode = pdf_extractor
        try:
            db.check_version(self.conn)
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    # ---- setup ----
    @classmethod
    def init(cls, root, packs=(DEFAULT_PACK,), actor="bootstrap", journal_mode="DELETE"):
        os.makedirs(root, exist_ok=True)
        k = cls.__new__(cls)
        k.root = os.path.abspath(root)
        k.db_path = os.path.join(k.root, "ukb.sqlite")
        if os.path.exists(k.db_path) and os.path.getsize(k.db_path) > 0:
            raise UKBError("ALREADY_INITIALIZED", "a database already exists here", path=k.db_path)
        k.blobs = BlobStore(os.path.join(k.root, "blobs"))
        k.conn = db.connect(k.db_path, journal_mode)
        k._pdf_mode = "auto"
        db.init_schema(k.conn)
        n = 0
        with db.tx(k.conn):
            for p in packs:
                n += registry.load_pack(k.conn, p, actor)
            audit.append(k.conn, actor, "init", "ukb", k.root, {"packs": [os.path.basename(p) for p in packs],
                                                                 "items": n})
        db.check_version(k.conn)
        return k

    def load_pack(self, pack_dir, actor):
        with db.tx(self.conn):
            return registry.load_pack(self.conn, pack_dir, actor)

    def _pdf(self):
        if self._pdf_mode is None:
            return None
        if callable(self._pdf_mode):
            return self._pdf_mode
        if not registry.setting(self.conn, "pdf_text_extraction"):
            return None
        from .plugins import pdf_text
        return pdf_text.extract if pdf_text.available() else None

    # ---- administrators ----
    def add_admin(self, admin_id, actor):
        with db.tx(self.conn):
            return review.add_admin(self.conn, admin_id, actor)

    def reset_token(self, admin_id, actor):
        with db.tx(self.conn):
            return review.reset_token(self.conn, admin_id, actor)

    def set_admin_active(self, admin_id, active, actor):
        with db.tx(self.conn):
            review.set_admin_active(self.conn, admin_id, active, actor)

    def auth(self, admin_id, token):
        return review.authenticate(self.conn, admin_id, token)

    # ---- AI contribution helpers (read-only) ----
    def guide(self, type_id=None):
        return contribute.guide(self.conn, type_id)

    def prepare(self, profile_id, type_id, tags):
        return contribute.prepare(self.conn, self.profile(profile_id), type_id, tags)

    def my_submissions(self, submitter, state=None, limit=50):
        return contribute.mine(self.conn, submitter, state, limit)

    # ---- write side ----
    def propose(self, payload, channel, submitter):
        try:
            with db.tx(self.conn):
                return _intake.propose(self.conn, self.blobs, payload, channel, submitter, self._pdf())
        except UKBError as err:
            with db.tx(self.conn):
                iid = _intake.record_invalid(self.conn, payload, channel, submitter, err)
            err.detail["intake_id"] = iid
            raise

    def prereview_bundle(self, task_id, page=0):
        return review.prereview_bundle(self.conn, self.blobs, task_id, page)

    def prereview_submit(self, task_id, reviewer_id, page_tokens, opinion):
        with db.tx(self.conn):
            return review.prereview_submit(self.conn, self.blobs, task_id, reviewer_id, page_tokens, opinion)

    def queue(self, state="open", kind=None, limit=200, needs=None):
        return review.queue(self.conn, state, kind, limit, needs)

    def review_view(self, task_id):
        return review.review_view(self.conn, task_id)

    def decide(self, task_id, admin_id, token, decision, rationale, bound_hash, channel, args=None):
        with db.tx(self.conn):
            who = review.authenticate(self.conn, admin_id, token)
            return review.decide(self.conn, self.blobs, task_id, who, decision, rationale, bound_hash, channel, args,
                                 self._pdf())

    def propose_status(self, entry_ref, target_status, reason, submitter, by_entry=None):
        with db.tx(self.conn):
            return review.propose_status(self.conn, entry_ref, target_status, reason, submitter, by_entry)

    def propose_tags(self, entry_ref, add, drop, reason, submitter):
        with db.tx(self.conn):
            return review.propose_tags(self.conn, entry_ref, add, drop, reason, submitter)

    def propose_registry(self, kind, spec, reason, submitter):
        with db.tx(self.conn):
            return review.propose_registry(self.conn, kind, spec, reason, submitter)

    def feedback(self, entry_ref, consumer, outcome, note=None, task_ref=None):
        with db.tx(self.conn):
            return review.feedback(self.conn, entry_ref, consumer, outcome, note, task_ref)

    # ---- read side ----
    def profile(self, profile_id):
        return registry.profile(self.conn, profile_id)

    def must_read(self, profile_id):
        return navigate.must_read(self.conn, self.profile(profile_id))

    def overview(self, profile_id, facet=None, within=None, parent=None):
        return navigate.overview(self.conn, self.profile(profile_id), facet, within, parent)

    def catalog(self, profile_id, scope=None, types=None, limit=None):
        return navigate.catalog(self.conn, self.profile(profile_id), scope, types, limit)

    def read(self, profile_id, scope=None, entry_refs=None, types=None, cursor=None, view="full"):
        return navigate.read(self.conn, self.profile(profile_id), scope, entry_refs, types, cursor, view)

    def read_original(self, profile_id, ref, path, section=None, page=None, lines=None, cursor=None):
        return navigate.read_original(self.conn, self.blobs, self.profile(profile_id), ref, path, section, page,
                                      lines, cursor)

    def checkout(self, profile_id, ref, dest, actor, paths=None, allow_non_reusable=False, overwrite=False,
                 task_ref=None):
        prof = self.profile(profile_id)
        with db.tx(self.conn):
            return navigate.checkout(self.conn, self.blobs, prof, ref, dest, actor, paths, allow_non_reusable,
                                     overwrite, task_ref)

    def types(self):
        out = []
        for t in self.conn.execute("SELECT type_id FROM reg_type WHERE status='active' ORDER BY type_id"):
            s = registry.type_spec(self.conn, t[0])
            out.append({"type_id": s["type_id"], "version": s["version"], "label": s["label"],
                        "description": s["description"], "spec": s["spec"]})
        return out

    # ---- maintenance ----
    def lint(self):
        return maintenance.lint(self.conn, self.blobs)

    def status(self):
        return maintenance.status(self.conn)

    def verify_audit(self):
        return audit.verify(self.conn)

    def export(self, dest_dir):
        return maintenance.export_jsonl(self.conn, dest_dir)

    def backup(self, dest_path):
        return maintenance.backup(self.conn, dest_path)
