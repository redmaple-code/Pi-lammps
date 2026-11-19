"""Document-tier entries: file intake checks, readable text, natural sections, locators, manifest hash.

Only structural checks. Which claims need a citation, and whether a summary is faithful, is judged by
AI prereview and the administrator.
"""
import base64
import json
import os
import re

from . import registry
from .canon import hash_obj, sha256_bytes
from .errors import UKBError

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SEG_RE = re.compile(r"^[^<>|?*\"]+$")
_MD_HEADING_RE = re.compile(r"^(#{1,4})[ \t]+(.+?)[ \t]*#*[ \t]*$")
LOCATOR_RE = re.compile(r"\[\[([^\[\]\n]{1,300})\]\]")
_LINES_RE = re.compile(r"^L(\d+)(?:-(\d+))?$")
_PAGE_RE = re.compile(r"^p(\d+)(?:-(\d+))?$")


def norm_path(p):
    if not isinstance(p, str):
        raise UKBError("FILE_PATH_INVALID", "path must be a string")
    p = p.strip()
    if p.startswith("./"):
        p = p[2:]
    if not p or len(p) > 255 or p.startswith("/") or "\\" in p or ":" in p or "//" in p or _CTRL_RE.search(p):
        raise UKBError("FILE_PATH_INVALID", "path must be a relative POSIX path inside the package", path=p)
    for seg in p.split("/"):
        if seg in ("", ".", "..") or not _SEG_RE.match(seg) or seg != seg.strip() or seg.endswith("."):
            raise UKBError("FILE_PATH_INVALID", "invalid path segment", path=p, segment=seg)
    if p.lower().startswith("ukb_provenance"):
        raise UKBError("FILE_PATH_INVALID", "reserved file name", path=p)
    return p


def _under(path, roots):
    rp = os.path.realpath(path)
    for r in roots:
        r = os.path.realpath(r)
        if rp == r or rp.startswith(r.rstrip(os.sep) + os.sep):
            return rp
    return None


def _load_bytes(spec, intake_roots, key_prefix=""):
    sp = spec.get(key_prefix + "source_path")
    ct = spec.get(key_prefix + "content_text")
    cb = spec.get(key_prefix + "content_b64")
    given = [x is not None for x in (sp, ct, cb)].count(True)
    if given != 1:
        raise UKBError("FILE_SOURCE_INVALID", "give exactly one of source_path, content_text, content_b64",
                       path=spec.get("path"))
    if ct is not None:
        if not isinstance(ct, str):
            raise UKBError("FILE_SOURCE_INVALID", "content_text must be a string")
        return ct.encode("utf-8")
    if cb is not None:
        try:
            return base64.b64decode(cb, validate=True)
        except Exception:
            raise UKBError("FILE_SOURCE_INVALID", "content_b64 is not valid base64", path=spec.get("path"))
    if not intake_roots:
        raise UKBError("FILE_SOURCE_DENIED", "reading local files requires configured intake_roots")
    rp = _under(sp, intake_roots)
    if rp is None:
        raise UKBError("FILE_SOURCE_DENIED", "source_path is outside intake_roots", source_path=sp)
    if not os.path.isfile(rp):
        raise UKBError("FILE_SOURCE_INVALID", "source_path is not a regular file", source_path=sp)
    with open(rp, "rb") as f:
        return f.read()


def _check_content(fmt, data, path):
    kind = fmt["kind"]
    if kind == "text":
        if b"\x00" in data:
            raise UKBError("FILE_CONTENT_INVALID", "text file contains NUL bytes", path=path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise UKBError("FILE_CONTENT_INVALID", "text file is not valid UTF-8", path=path)
        if text.startswith("\ufeff"):
            text = text[1:]
        return text
    magic = fmt["magic_hex"]
    if kind == "pdf" and not magic:
        magic = "25504446"
    if magic and not data.startswith(bytes.fromhex(magic)):
        raise UKBError("FILE_CONTENT_INVALID", "file content does not match its declared format", path=path,
                       format=fmt["format_id"])
    return None


def markdown_sections(text):
    lines = text.split("\n")
    starts = []
    pos = 0
    in_fence = False
    for ln in lines:
        s = ln.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
        elif not in_fence:
            m = _MD_HEADING_RE.match(ln)
            if m:
                starts.append((pos, m.group(2).strip()))
        pos += len(ln) + 1
    out = []
    for i, (st, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        if end > st:
            out.append({"section_id": "s%d" % (i + 1), "title": title[:200], "start_char": st, "end_char": end})
    return out


def line_sections(text, given, path):
    if not isinstance(given, list) or not given:
        raise UKBError("SECTION_INVALID", "sections must be a non-empty list", path=path)
    offs = [0]
    for ln in text.split("\n"):
        offs.append(offs[-1] + len(ln) + 1)
    nlines = len(offs) - 1
    out = []
    prev_end = 0
    for i, s in enumerate(given):
        try:
            title = str(s["title"]).strip()
            a, b = int(s["start_line"]), int(s["end_line"])
        except Exception:
            raise UKBError("SECTION_INVALID", "section needs title, start_line, end_line", path=path, index=i)
        if not title or a < 1 or b < a or b > nlines:
            raise UKBError("SECTION_INVALID", "section line range out of bounds", path=path, index=i)
        st, en = offs[a - 1], min(offs[b], len(text))
        if st < prev_end:
            raise UKBError("SECTION_INVALID", "sections overlap or are out of order", path=path, index=i)
        prev_end = en
        out.append({"section_id": "s%d" % (i + 1), "title": title[:200], "start_char": st, "end_char": en})
    return out


def prepare_files(conn, blobs, tspec, files_in, parent_files, intake_roots, pdf_extractor):
    """Returns list of file records ready to persist. Writes blobs (content-addressed, idempotent)."""
    doc = tspec["spec"]["document"]
    allowed_roles = set(doc["allowed_roles"])
    allowed_formats = set(doc["allowed_formats"]) if doc.get("allowed_formats") else None
    max_file = registry.setting(conn, "max_file_bytes")
    max_total = min(registry.setting(conn, "max_entry_bytes"), doc.get("max_total_bytes") or 1 << 62)
    max_files = min(registry.setting(conn, "max_files_per_entry"), doc.get("max_files") or 1 << 30)
    if files_in is None:
        if parent_files is None:
            raise UKBError("FILES_REQUIRED", "document entries need files")
        files_in = [{"path": f["path"], "keep": True} for f in parent_files]
    if not isinstance(files_in, list) or not files_in:
        raise UKBError("FILES_REQUIRED", "document entries need a non-empty files list")
    if len(files_in) > max_files:
        raise UKBError("FILE_LIMIT", "too many files", max_files=max_files)
    parent_by_path = {f["path"]: f for f in (parent_files or [])}
    out, seen, total = [], set(), 0
    for spec in files_in:
        if not isinstance(spec, dict):
            raise UKBError("FILE_SOURCE_INVALID", "file entry must be an object")
        path = norm_path(spec.get("path"))
        if path.lower() in seen:
            raise UKBError("FILE_PATH_INVALID", "duplicate path", path=path)
        seen.add(path.lower())
        if spec.get("keep"):
            pf = parent_by_path.get(path)
            if pf is None:
                raise UKBError("FILE_SOURCE_INVALID", "keep refers to a file not in the parent revision", path=path)
            rec = dict(pf)
            for k in ("description", "reusable", "license_note"):
                if k in spec:
                    rec[k] = spec[k]
            if "role" in spec:
                rec["role_id"] = spec["role"]
        else:
            fmt = registry.match_file_format(conn, path)
            data = _load_bytes(spec, intake_roots)
            limit = min(max_file, fmt["max_bytes"] or max_file)
            if len(data) > limit:
                raise UKBError("FILE_TOO_LARGE", "file exceeds the size limit; large raw outputs stay outside "
                               "the knowledge base", path=path, size=len(data), limit=limit)
            text = _check_content(fmt, data, path)
            sha = blobs.put_bytes(data)
            rec = {"path": path, "format_id": fmt["format_id"], "sha256": sha, "size": len(data),
                   "text_sha256": None, "char_count": None, "line_count": None, "page_count": None,
                   "sections": [], "_kind": fmt["kind"]}
            if fmt["kind"] == "text":
                if text.strip():
                    rec["text_sha256"] = sha if text.encode("utf-8") == data else blobs.put_bytes(text.encode("utf-8"))
                    rec["char_count"] = len(text)
                    rec["line_count"] = text.count("\n") + (0 if text.endswith("\n") else 1)
                    if spec.get("sections") is not None:
                        rec["sections"] = line_sections(text, spec["sections"], path)
                    elif path.lower().endswith((".md", ".markdown")):
                        rec["sections"] = markdown_sections(text)
            elif fmt["kind"] == "pdf":
                ptext = None
                if any(spec.get(k) is not None for k in ("text_source_path", "text_content_text", "text_content_b64")):
                    tb = _load_bytes(spec, intake_roots, key_prefix="text_")
                    try:
                        ptext = tb.decode("utf-8")
                    except UnicodeDecodeError:
                        raise UKBError("FILE_CONTENT_INVALID", "PDF text is not valid UTF-8", path=path)
                elif pdf_extractor is not None:
                    ptext = pdf_extractor(data)
                if ptext and ptext.strip():
                    rec["text_sha256"] = blobs.put_bytes(ptext.encode("utf-8"))
                    rec["char_count"] = len(ptext)
                    rec["page_count"] = ptext.count("\f") + 1
            rec["role_id"] = spec.get("role")
            rec["description"] = spec.get("description")
            rec["reusable"] = spec.get("reusable")
            rec["license_note"] = spec.get("license_note")
        role_id = rec.get("role_id")
        if role_id not in allowed_roles:
            raise UKBError("FILE_ROLE_INVALID", "role not allowed for this type", path=path, role=role_id,
                           allowed=sorted(allowed_roles))
        role = conn.execute("SELECT * FROM reg_artifact_role WHERE role_id=? AND status='active'",
                            (role_id,)).fetchone()
        if role is None:
            raise UKBError("FILE_ROLE_INVALID", "role not registered or inactive", role=role_id)
        if allowed_formats is not None and rec["format_id"] not in allowed_formats:
            raise UKBError("FILE_TYPE_NOT_ALLOWED", "file format not allowed for this type", path=path,
                           format=rec["format_id"])
        if role["formats_json"]:
            rf = json.loads(role["formats_json"])
            if rec["format_id"] not in rf:
                raise UKBError("FILE_TYPE_NOT_ALLOWED", "file format not allowed for this role", path=path,
                               role=role_id, format=rec["format_id"])
        desc = rec.get("description")
        if not isinstance(desc, str) or not desc.strip():
            raise UKBError("FILE_DESCRIPTION_REQUIRED", "every file needs a description", path=path)
        rec["description"] = desc.strip()
        if rec.get("reusable") is None:
            rec["reusable"] = bool(role["default_reusable"])
        if not isinstance(rec["reusable"], (bool, int)):
            raise UKBError("FILE_SOURCE_INVALID", "reusable must be boolean", path=path)
        rec["reusable"] = bool(rec["reusable"])
        total += rec["size"]
        if total > max_total:
            raise UKBError("FILE_LIMIT", "total size of files exceeds the limit", limit=max_total)
        rec["path"] = path
        out.append(rec)
    present = {r["role_id"] for r in out}
    missing = [r for r in doc.get("required_roles", []) if r not in present]
    if missing:
        raise UKBError("FILE_ROLE_MISSING", "required file roles are missing", roles=missing)
    out.sort(key=lambda r: r["path"])
    return out


def manifest_hash(files):
    return hash_obj([{
        "path": f["path"], "role": f["role_id"], "format": f["format_id"], "sha256": f["sha256"],
        "size": f["size"], "text_sha256": f["text_sha256"], "reusable": bool(f["reusable"]),
        "description": f["description"], "license_note": f.get("license_note"),
        "sections": [[s["section_id"], s["title"], s["start_char"], s["end_char"]] for s in f["sections"]],
    } for f in sorted(files, key=lambda x: x["path"])])


def parse_locator(raw):
    raw = raw.strip()
    if "#" in raw:
        path, frag = raw.split("#", 1)
        kind = "page" if _PAGE_RE.match(frag) else "section"
        return {"path": path.strip(), "kind": kind, "value": frag.strip()}
    m = re.match(r"^(.*?):(L\d+(?:-\d+)?)$", raw)
    if m:
        return {"path": m.group(1).strip(), "kind": "lines", "value": m.group(2)}
    return {"path": raw, "kind": "file", "value": None}


def check_locator(loc, files_by_path):
    f = files_by_path.get(loc["path"])
    if f is None:
        return "file not in package"
    if loc["kind"] == "file":
        return None
    if loc["kind"] == "section":
        if not any(s["section_id"] == loc["value"] for s in f["sections"]):
            return "unknown section"
        return None
    if loc["kind"] == "page":
        m = _PAGE_RE.match(loc["value"])
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not f.get("page_count"):
            return "file has no readable pages"
        if a < 1 or b < a or b > f["page_count"]:
            return "page out of range"
        return None
    m = _LINES_RE.match(loc["value"])
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if not f.get("line_count"):
        return "file has no readable lines"
    if a < 1 or b < a or b > f["line_count"]:
        return "line range out of bounds"
    return None


def check_summary(tspec, text_values, files):
    doc = tspec["spec"]["document"]
    total = sum(len(t) for t in text_values)
    if total > doc["summary_max_chars"]:
        raise UKBError("SUMMARY_TOO_LONG", "summary layer exceeds summary_max_chars", chars=total,
                       limit=doc["summary_max_chars"])
    by_path = {f["path"]: f for f in files}
    locs, bad = [], []
    for t in text_values:
        for m in LOCATOR_RE.finditer(t):
            loc = parse_locator(m.group(1))
            problem = check_locator(loc, by_path)
            if problem:
                bad.append({"locator": m.group(0), "problem": problem})
            locs.append(m.group(0))
    if bad:
        raise UKBError("LOCATOR_INVALID", "summary locators do not resolve to the package", issues=bad)
    if doc.get("summary_must_cite") and not locs:
        raise UKBError("LOCATOR_REQUIRED", "this type requires the summary to cite the original with [[path...]]")
    return total


def readable_text(blobs, f):
    if not f["text_sha256"]:
        return None
    return blobs.read_text(f["text_sha256"])


def slice_text(text, f, sections, section=None, page=None, lines=None):
    """Returns (label, text). Exactly one selector or none."""
    sel = [x is not None for x in (section, page, lines)].count(True)
    if sel > 1:
        raise UKBError("SELECTOR_INVALID", "use only one of section, page, lines")
    if section is not None:
        for s in sections:
            if s["section_id"] == section:
                return "%s#%s %s" % (f["path"], section, s["title"]), text[s["start_char"]:s["end_char"]]
        raise UKBError("SECTION_UNKNOWN", "section not found", section=section)
    if page is not None:
        m = _PAGE_RE.match(str(page)) or _PAGE_RE.match("p%s" % page)
        if not m:
            raise UKBError("SELECTOR_INVALID", "page must look like 3 or p3 or p3-5")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        pages = text.split("\f")
        if a < 1 or b < a or b > len(pages):
            raise UKBError("SELECTOR_INVALID", "page out of range", pages=len(pages))
        return "%s#p%d-%d" % (f["path"], a, b), "\f".join(pages[a - 1:b])
    if lines is not None:
        m = _LINES_RE.match(str(lines)) or _LINES_RE.match("L%s" % lines)
        if not m:
            raise UKBError("SELECTOR_INVALID", "lines must look like L10-20")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        ls = text.split("\n")
        if a < 1 or b < a or b > len(ls):
            raise UKBError("SELECTOR_INVALID", "line range out of bounds", lines=len(ls))
        return "%s:L%d-%d" % (f["path"], a, b), "\n".join(ls[a - 1:b])
    return f["path"], text


def raw_sha(data):
    return sha256_bytes(data)
