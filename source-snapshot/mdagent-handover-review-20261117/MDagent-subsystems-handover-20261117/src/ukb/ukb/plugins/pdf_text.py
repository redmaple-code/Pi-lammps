"""Optional PDF text extraction. Tries PyMuPDF, then pypdf; returns None if neither yields clean text.

Pages are separated by form feed so page locators (#p3) can be resolved. Output that is mostly control or
replacement characters (typical of fonts without a ToUnicode map) is treated as a failure rather than stored.
"""
import io


def _pymupdf(data):
    try:
        import fitz
    except Exception:
        return None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            return [p.get_text() for p in doc]
        finally:
            doc.close()
    except Exception:
        return None


def _pypdf(data):
    try:
        import pypdf
    except Exception:
        return None
    try:
        return [(p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(data)).pages]
    except Exception:
        return None


def _clean(pages):
    text = "\f".join(p.replace("\f", " ") for p in pages)
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return None
    bad = sum(1 for c in visible if c == "\ufffd" or (ord(c) < 32) or 0xE000 <= ord(c) <= 0xF8FF)
    if bad / len(visible) > 0.05:
        return None
    return text


def available():
    for mod in ("fitz", "pypdf"):
        try:
            __import__(mod)
            return True
        except Exception:
            continue
    return False


def extract(data):
    for fn in (_pymupdf, _pypdf):
        pages = fn(data)
        if pages is None:
            continue
        text = _clean(pages)
        if text is not None:
            return text
    return None
