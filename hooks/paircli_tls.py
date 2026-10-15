"""Build verified TLS contexts compatible with managed corporate CAs."""
from __future__ import annotations

import ssl


def compatible_tls_context() -> ssl.SSLContext:
    """Keep certificate verification while accepting legacy CA key-usage metadata."""
    context = ssl.create_default_context()
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        context.verify_flags &= ~strict
    return context
