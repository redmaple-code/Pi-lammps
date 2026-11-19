"""Content-addressed blob store: <root>/<sha[:2]>/<sha>, written via tmp + os.replace."""
import hashlib
import os
import re
import tempfile

from .errors import UKBError

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class BlobStore:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def path(self, sha):
        if not _SHA_RE.match(sha or ""):
            raise UKBError("BLOB_INVALID", "invalid blob hash", sha=sha)
        return os.path.join(self.root, sha[:2], sha)

    def exists(self, sha):
        return os.path.isfile(self.path(sha))

    def put_bytes(self, data):
        sha = hashlib.sha256(data).hexdigest()
        dest = self.path(sha)
        if os.path.isfile(dest) and os.path.getsize(dest) == len(data):
            return sha
        d = os.path.dirname(dest)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return sha

    def read_bytes(self, sha):
        p = self.path(sha)
        try:
            with open(p, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise UKBError("BLOB_MISSING", "blob not found in store", sha=sha)
        if hashlib.sha256(data).hexdigest() != sha:
            raise UKBError("BLOB_CORRUPT", "blob content does not match its hash", sha=sha)
        return data

    def read_text(self, sha):
        return self.read_bytes(sha).decode("utf-8")

    def iter_all(self):
        for sub in sorted(os.listdir(self.root)):
            d = os.path.join(self.root, sub)
            if os.path.isdir(d) and len(sub) == 2:
                for name in sorted(os.listdir(d)):
                    if _SHA_RE.match(name):
                        yield name
