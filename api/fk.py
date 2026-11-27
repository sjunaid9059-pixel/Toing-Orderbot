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
  * Per-request proxy override is supported ONLY with a valid relay
    key: header X-Relay-Proxy with a proxy URL (or host:port:user:pass).
    The override is the only route tried for that request — no silent
    fallback — so the caller can rotate proxies on a 502/403. The
    server env proxy (TOING_PROXY/TOING_PROXIES) is used when no
    override is sent.

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
from _fk_lib import (_normalize_proxy_url, fk_http_json, proxy_status,
                     send_json)

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

# Numbered Rome hosts the Minutes app uses for its page-flow
# (e.g. 1.rome.api.flipkart.com for /api/4/page/fetch). Any
# <label>.rome.api.flipkart.<tld> is allowed, not just the bare host.
def _host_allowed(host):
    host = (host or "").lower()
    if host in ALLOWED_HOSTS:
        return True
    for suffix in (".rome.api.flipkart.net", ".rome.api.flipkart.com"):
        if host.endswith(suffix) and host[: -len(suffix)]:
            return True
    return False

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


def _connect_probe(proxy_url):
    """Raw CONNECT to the proxy gateway, returning its exact reply.

    proxy_url is the normalized http://user:pass@host:port. Sends two
    CONNECTs: one without auth, one with Basic auth. Returns the raw
    response text of each (up to 2KB), with the password and the
    base64(user:pass) token redacted. Never returns credentials."""
    import base64
    import socket
    out = {"ok": True, "probe": "connect"}
    if not proxy_url:
        out.update({"ok": False, "error": "no proxy override given"})
        return out
    try:
        u = urllib.parse.urlparse(proxy_url)
        host, port = u.hostname, u.port or 443
        user = urllib.parse.unquote(u.username or "")
        pwd = urllib.parse.unquote(u.password or "")
    except Exception:
        out.update({"ok": False, "error": "bad proxy"})
        return out

    def _redact(text):
        for secret in (pwd, base64.b64encode(
                ("%s:%s" % (user, pwd)).encode()).decode()):
            if secret and secret in text:
                text = text.replace(secret, "***")
        return text

    def _one(with_auth):
        try:
            s = socket.create_connection((host, port), timeout=8)
            s.settimeout(8)
            lines = ["CONNECT rome.api.flipkart.net:443 HTTP/1.1",
                     "Host: rome.api.flipkart.net:443"]
            if with_auth:
                cred = base64.b64encode(
                    ("%s:%s" % (user, pwd)).encode()).decode()
                lines.append("Proxy-Authorization: Basic " + cred)
            lines += ["Proxy-Connection: Keep-Alive", "", ""]
            s.sendall("\r\n".join(lines).encode())
            data = b""
            try:
                while len(data) < 2048:
                    chunk = s.recv(2048)
                    if not chunk:
                        break
                    data += chunk
                    if b"\r\n\r\n" in data and len(data) > 200:
                        break
            except Exception:
                pass
            s.close()
            return _redact(data.decode("utf-8", errors="replace")[:2048])
        except Exception as e:
            return "%s: %s" % (type(e).__name__, str(e)[:120])

    out["noAuth"] = _one(False)
    out["withAuth"] = _one(True)
    return out


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
        if parsed.scheme != "https" or not _host_allowed(parsed.hostname):
            send_json(self, 403, {"ok": False, "error": "host not allowed"})
            return
        headers = _clean_headers(payload.get("headers") or {})
        body = payload.get("body")
        if body is not None and not isinstance(body, (dict, list)):
            send_json(self, 400, {"ok": False, "error": "body must be JSON"})
            return
        # Key-gated per-request proxy override: only the relay-key holder
        # (the bot) may choose the exit proxy. The value is normalized
        # (userinfo percent-encoded) and validated; a bad value is 400.
        # When an override is set it is the ONLY route — no silent
        # fallback to direct, so the caller can rotate on a 502/403.
        override_raw = (self.headers.get("X-Relay-Proxy") or "").strip()
        override = None
        if override_raw:
            if len(override_raw) > 512:
                send_json(self, 400, {"ok": False, "error": "bad proxy"})
                return
            override = _normalize_proxy_url(override_raw)
            if not override:
                send_json(self, 400, {"ok": False, "error": "bad proxy"})
                return
        # Raw CONNECT probe (key-gated): open a plain socket to the proxy
        # gateway, send CONNECT with/without Basic auth, and return the
        # gateway's exact reply (credentials redacted). This is the only
        # way to see WHY the gateway 407s — curl/urllib hide the body.
        if payload.get("probe") == "connect":
            send_json(self, 200, _connect_probe(override))
            return
        status, data, raw, resp_headers, via_proxy, attempts = fk_http_json(
            method, url, headers=headers, body=body, timeout=20,
            proxy_override=override)
        if status is None:
            send_json(self, 502, {"ok": False, "error": raw[:300],
                                  "viaProxy": via_proxy, "routes": attempts})
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
                              "routes": attempts,
                              "setCookies": set_cookies[:10]})

    def log_message(self, *args):
        pass
