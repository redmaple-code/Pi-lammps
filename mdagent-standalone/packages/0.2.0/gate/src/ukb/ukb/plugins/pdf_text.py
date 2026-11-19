"""No bundled PDF parser; inject a trusted extractor explicitly."""
def available(): return False
def extract(data): return None
