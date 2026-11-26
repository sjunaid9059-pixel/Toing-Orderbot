"""POST /api/fk -> Flipkart relay (for the @FKMINSAUTOMATIONbot on the VM).

The VM cannot reach Flipkart's login API: direct calls get HTTP 403
(Flipkart WAF blocks datacentre IPs) and the residential proxy gateway
is unreachable from the VM (its network resets the connection). This
Vercel function runs the same request through the residential proxy
(TOING_PROXIES / TOING_PROXY env vars, already set on this project —
the Toing order bot proves Vercel reaches that proxy fine) and returns
Flipkart's real response to the bot.

Security — the relay is NOT an open proxy:
  * POST requires header X-Relay-Key. The raw key lives only in the
    bot's private .env on the VM; this file holds only its SHA-256
    hex, which cannot be reversed into the key. Wrong/missing key
    gets 401 and nothing is forwarded.
  * Only https URLs on Flipkart hosts are forwarded (allowlist below).
  * Only GET/POST, bounded sizes (URL 2KB, body 64KB, response 512KB).
  * No request or response logging of bodies, cookies or credentials.
  * Per-request proxy override is NOT supported here — the server env
    proxy is the only route (plus direct fallback inside the lib).

GET /api/fk -> status only (no auth): {ok, relay, proxyConfigured,
proxyHost (masked), proxySource}. No credentials in any response.
"""
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _fk_lib import fk_http_json, proxy_status, send_json

# SHA-256 hex of the relay key. The raw key is in the bot's private
# .env (FK_RELAY_KEY) and nowhere else. Rotate by generating a new key,
# replacing this hash, updating the bot .env, and redeploying.
RELAY_KEY_SHA256 = "d42c0e156abe7f8749239b0312bf506817521545e95adefc12d091162deea0e1"

ALLOWED_HOSTS = {
    "rome.api.flipkart.net",
    "rome.api.flipkart.com",
    "www.flipkart.com",
    "minutes.flipkart.com",
    "m.flipkart.com",
}

MAX_URL_LEN = 2048
MAX_BODY_LEN = 65536
MAX_RESP_LEN = 524288

# Headers the caller may not smuggle through the relay.
_BLOCKED_REQ_HEADERS = {
    "host", "content-length", "connection", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
}


def _key_ok(handler):
    """Constant-time check of X-Relay-Key against the stored hash."""
    raw = (handler.headers.get("X-Relay-Key") or "").strip()
    if not raw or not RELAY_KEY_SHA256 or "PLACEHOLDER" in RELAY_KEY_SHA256:
        return False
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, RELAY_KEY_SHA256.lower())


def _clean_headers(headers):
    """Keep caller headers, minus hop-by-hop / proxy headers."""
    out = {}
    if not isinstance(headers, dict):
        return out
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.lower() in _BLOCKED_REQ_HEADERS:
            continue
        if len(k) > 64 or len(v) > 4096:
            continue
        out[k] = v
    return out


def _read_body(handler):
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except Exception:
        return None, "bad Content-Length"
    if n <= 0 or n > MAX_BODY_LEN:
        return None, "body too large or empty"
    try:
        raw = handler.rfile.read(n)
    except Exception:
        return None, "read failed"
    try:
        return json.loads(raw.decode("utf-8")), ""
    except Exception:
        return None, "body is not JSON"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        st = proxy_status()
        send_json(self, 200, {"ok": True, "relay": "fk",
                              "proxyConfigured": st["configured"],
                              "proxyHost": st["host"],
                              "proxySource": st["source"]})

    def do_POST(self):
        if not _key_ok(self):
            send_json(self, 401, {"ok": False, "error": "unauthorized"})
            return
        payload, err = _read_body(self)
        if payload is None:
            send_json(self, 400, {"ok": False, "error": err})
            return
        method = str(payload.get("method") or "GET").upper()
        url = str(payload.get("url") or "")
        if method not in ("GET", "POST"):
            send_json(self, 400, {"ok": False, "error": "method not allowed"})
            return
        if not url or len(url) > MAX_URL_LEN:
            send_json(self, 400, {"ok": False, "error": "bad url"})
            return
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            send_json(self, 400, {"ok": False, "error": "bad url"})
            return
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
            send_json(self, 403, {"ok": False, "error": "host not allowed"})
            return
        headers = _clean_headers(payload.get("headers") or {})
        body = payload.get("body")
        if body is not None and not isinstance(body, (dict, list)):
            send_json(self, 400, {"ok": False, "error": "body must be JSON"})
            return
        status, data, raw, resp_headers, via_proxy = fk_http_json(
            method, url, headers=headers, body=body, timeout=20)
        if status is None:
            send_json(self, 502, {"ok": False, "error": raw[:300],
                                  "viaProxy": via_proxy})
            return
        raw = (raw or "")[:MAX_RESP_LEN]
        set_cookies = []
        try:
            for k, v in (resp_headers or {}).items():
                if k.lower() == "set-cookie":
                    set_cookies.append(v)
        except Exception:
            pass
        send_json(self, 200, {"ok": True, "status": status, "data": data,
                              "raw": raw, "viaProxy": via_proxy,
                              "setCookies": set_cookies[:10]})

    def log_message(self, *args):
        pass
