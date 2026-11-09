"""Bounded GET with finite retries that honours Retry-After. Credentials only travel in headers."""
import ssl
import time
import urllib.error
import urllib.request

import truststore

# Verify with the OS trust store (on this machine an antivirus re-signs some TLS traffic with a root
# that only the OS store holds). Verification stays on.
_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
# Wikimedia throttles generic library agents; identify the tool instead (no personal contact data).
UA = "ArabSportsCarouselDemo/0.1 (local research demo)"
RETRYABLE = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


def get(url, *, headers=None, timeout=30, retries=2, data=None):
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                wait = int(e.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            reason = f"HTTP {e.code}" + (f" (Retry-After {wait}s)" if wait else "")
            # Honour provider backoff: a long Retry-After ends the call instead of being shortened.
            if e.code not in RETRYABLE or attempt == retries or wait > 60:
                raise FetchError(reason) from None
            wait = wait or 5 * 2 ** attempt
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            if attempt == retries:
                raise FetchError(reason) from None
            wait = 2 * 2 ** attempt
        time.sleep(wait)
