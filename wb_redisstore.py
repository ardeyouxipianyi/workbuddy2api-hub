"""Optional Upstash Redis mirror for sticky sessions (panel parity).

The gateway keeps session -> account affinity in memory (SessionAffinity).
The panel project mirrors those bindings to Redis so a restart does not lose
stickiness; this module brings the same option here, standard library only.
Disabled by default: an install that never configures a URL keeps the pure
in-memory behaviour.

Failure policy: every Redis call is best-effort. A timeout, HTTP error or a
malformed reply degrades to "no mirror" (reads miss, writes drop) and must
never break a chat request.
"""
import json
import urllib.request

PREFIX = "wbaff:"


class UpstashStore(object):
    """Minimal Upstash REST client: GET/SET/DEL with plain string values."""

    def __init__(self, url, token=None, timeout=2.0):
        url = str(url or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        self.url = url
        self.token = str(token or "").strip()
        try:
            self.timeout = float(timeout or 2.0)
        except (TypeError, ValueError):
            self.timeout = 2.0

    def enabled(self):
        return bool(self.url)

    def _command(self, args):
        if not self.enabled():
            return None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, data=json.dumps(args).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return None
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return None

    def get(self, key):
        result = self._command(["GET", key])
        return result if isinstance(result, str) else None

    def set(self, key, value, ttl):
        try:
            ttl = max(1, int(ttl))
        except (TypeError, ValueError):
            ttl = 604800
        return self._command(["SET", key, str(value), "EX", ttl]) is not None

    def delete(self, key):
        return self._command(["DEL", key]) is not None


class NoopStore(object):
    """Mirror placeholder: everything misses and writes drop."""

    def enabled(self):
        return False

    def get(self, key):
        return None

    def set(self, key, value, ttl):
        return False

    def delete(self, key):
        return False


def build_mirror(url, token=None, timeout=2.0):
    url = str(url or "").strip()
    if not url:
        return NoopStore()
    return UpstashStore(url, token, timeout=timeout)
