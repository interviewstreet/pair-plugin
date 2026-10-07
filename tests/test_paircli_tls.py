import ssl
import sys
from pathlib import Path


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from paircli_tls import compatible_tls_context


def test_compatible_tls_context_keeps_verification_enabled():
    context = compatible_tls_context()

    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    if strict := getattr(ssl, "VERIFY_X509_STRICT", 0):
        assert context.verify_flags & strict == 0
