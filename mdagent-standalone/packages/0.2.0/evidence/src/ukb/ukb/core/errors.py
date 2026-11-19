"""Stable error type. Every refusal carries a machine-readable code."""


class UKBError(Exception):
    def __init__(self, code, message, **detail):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self):
        out = {"ok": False, "error": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out
