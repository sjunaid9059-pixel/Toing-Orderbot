"""Shared helpers for the Flipkart relay (Vercel Python serverless).

Slimmed copy of the Toing Mini backend's proxy + HTTP pattern
(api/_lib.py in this same project): outbound calls go through the
residential proxy pool when configured (TOING_PROXIES / TOING_PROXY env
vars — already set on this Vercel project), then fall back to direct.
Transport is curl_cffi (Chrome 124 browser impersonation) with a stdlib
urllib fallback, so the backend never crashes on a missing wheel.

No Swiggy/Toing specifics live here — this lib only moves bytes for
Flipkart hosts. The relay handler (api/fk.py) enforces the host
allowlist and the relay-key auth; nothing in this file is public by
itself. Never log or return a proxy URL or credential.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- Proxy selection

def _proxy_pool():
    """TOING_PROXIES: comma-separated proxy pool (Vercel env).
    Returns the list in order, stripped, duplicates removed."""
    pool = []
    for p in (os.environ.get("TOING_PROXIES") or "").split(","):
        p = p.strip()
        if p and p not in pool:
            pool.append(p)
    return pool


def _proxy_url():
    """Single default proxy (TOING_PROXY env). Unset/empty = direct."""
    v = (os.environ.get("TOING_PROXY") or "").strip()
    return v or None


def _normalize_proxy_url(raw):
    """Normalize a proxy string to http://user:pass@host:port with the
    userinfo percent-encoded. ProxyRise usernames contain a space
    ('res-indian -sid-123') — a raw space breaks the URL and the gateway
    answers 407. Accepts http(s):// URLs, user:pass@host:port, and
    host:port:user:pass. Returns None when unparsable."""
    if not raw or not isinstance(raw, str):
        return None
    p = raw.strip()
    if not p:
        return None
    if p.startswith(("http://", "https://")):
        try:
            u = urllib.parse.urlparse(p)
            if not u.hostname:
                return None
            auth = ""
            if u.username:
                auth = urllib.parse.quote(u.username, safe="%-._~")
                if u.password is not None:
                    auth += ":" + urllib.parse.quote(u.password, safe="%-._~")
                auth += "@"
            port = ":%d" % u.port if u.port else ""
            return "%s://%s%s%s" % (u.scheme.lower(), auth, u.hostname, port)
        except Exception:
            return None
    if "@" in p:
        return _normalize_proxy_url("http://" + p)
    parts = p.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        try:
            int(port)
        except Exception:
            return None
        if not host or not user:
            return None
        return "http://%s:%s@%s:%s" % (
            urllib.parse.quote(user, safe="%-._~"),
            urllib.parse.quote(pwd, safe="%-._~"), host, port)
    return None


def _proxy_candidates(override=None):
    """Ordered proxy URLs to try for one request, then None (direct).
    Pool capped at 3 entries per request so a long dead pool cannot
    eat the serverless budget; the direct attempt always happens last.
    When an override is given, it is the ONLY candidate — no silent
    fallback to direct, so the caller gets a clear dead-proxy signal
    (502) and can rotate to the next proxy itself."""
    if override:
        norm = _normalize_proxy_url(override)
        return [norm] if norm else []
    cands = []

    def _add(p):
        p = (p or "").strip()
        if p and p not in cands:
            cands.append(p)

    for p in _proxy_pool()[:3]:
        _add(p)
    _add(_proxy_url())
    cands.append(None)
    return cands


def _masked_proxy_host(proxy_url):
    """Hostname only (e.g. gw.proxyrise.com) — never user/pass/port/URL."""
    if not proxy_url:
        return None
    try:
        return urllib.parse.urlparse(proxy_url).hostname or None
    except Exception:
        return None


def proxy_configured():
    return bool(_proxy_url() or _proxy_pool())


def proxy_status():
    """Credentials-free proxy status for the relay status endpoint."""
    cands = _proxy_candidates()
    first = cands[0] if cands else None
    return {"configured": bool(first),
            "host": _masked_proxy_host(first),
            "source": "pool" if first and first in _proxy_pool() else
                      ("env" if first else "none")}


# ---------------------------------------------------------------- curl_cffi (browser impersonation)

_CFFI_SESSION_CLS = None  # lazy: False when curl_cffi is unavailable


def _cffi_session_cls():
    """curl_cffi Session class, or None when the package is not installed."""
    global _CFFI_SESSION_CLS
    if _CFFI_SESSION_CLS is None:
        try:
            from curl_cffi.requests import Session as _Cls
            _CFFI_SESSION_CLS = _Cls
        except Exception:
            _CFFI_SESSION_CLS = False
    return _CFFI_SESSION_CLS or None


def _cffi_attempt(method, url, headers=None, data=None, timeout=20, proxy=None):
    """ONE curl_cffi attempt via `proxy` (None = direct), Chrome 124
    impersonation. Returns (status, text, headers_dict, final_url), or
    None when curl_cffi is unavailable. Raises on transport errors —
    the caller moves to the next candidate. An HTTP error status is a
    real response, not an exception: returned, never retried."""
    cls = _cffi_session_cls()
    if cls is None:
        return None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    sess = cls(impersonate="chrome124", timeout=timeout)
    try:
        resp = sess.request(method, url, headers=headers or {}, data=data,
                            proxies=proxies, allow_redirects=True)
        return resp.status_code, resp.text, dict(resp.headers), str(resp.url)
    finally:
        try:
            sess.close()
        except Exception:
            pass


def _urllib_json_attempt(method, url, headers=None, data=None, timeout=20, proxy=None):
    """ONE urllib attempt via `proxy` (None = direct), returning
    (status, parsed, raw, headers). Raises on transport errors; an
    HTTPError status is a real response and is returned."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = None
    if proxy:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
        try:
            u = urllib.parse.urlparse(proxy)
            if u.username:
                pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                pm.add_password(None, u.hostname or "",
                                urllib.parse.unquote(u.username),
                                urllib.parse.unquote(u.password or ""))
                handlers.append(urllib.request.ProxyBasicAuthHandler(pm))
        except Exception:
            pass
        opener = urllib.request.build_opener(*handlers)
    try:
        resp = (opener.open(req, timeout=timeout) if opener
                else urllib.request.urlopen(req, timeout=timeout))
        with resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = None
            return resp.status, parsed, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        return e.code, parsed, raw, dict(e.headers)


# ---------------------------------------------------------------- HTTP helper

def _safe_err(e, proxy):
    """Short error text for diagnostics, with any proxy credential parts
    redacted (username/password/hostname never leave the server)."""
    msg = str(e)[:200]
    if proxy:
        try:
            u = urllib.parse.urlparse(proxy)
            for part in (u.username, u.password, u.hostname):
                if part and part in msg:
                    msg = msg.replace(part, "***")
        except Exception:
            pass
    return msg


def fk_http_json(method, url, headers=None, body=None, timeout=20, proxy_override=None):
    """One HTTP call returning (status, parsed_json_or_None, raw_text,
    resp_headers, via_proxy_bool, attempts). Never raises for HTTP error
    statuses. Returns status None only on a network failure. `body` is
    serialised as JSON when given. Proxy order: TOING_PROXIES pool ->
    TOING_PROXY -> direct. A transport failure on one route moves to the
    next candidate; the direct attempt is always tried last (time
    permitting), so a dead proxy never takes the call down. curl_cffi
    (Chrome 124) first, urllib fallback on the direct route.
    `attempts` lists per-route outcomes (via/status/error-class only —
    never URLs or credentials) so the caller can see why a route failed.
    When proxy_override is set, it is the only route tried."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    deadline = time.monotonic() + timeout
    last_err = None
    tried_proxy = False
    attempts = []
    for proxy in _proxy_candidates(proxy_override):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        if proxy:
            tried_proxy = True
            attempt_timeout = min(remaining, 8)
        else:
            attempt_timeout = remaining
        direct = proxy is None
        res = None
        try:
            res = _cffi_attempt(method, url, headers=headers, data=data,
                                timeout=attempt_timeout, proxy=proxy)
        except Exception as e:
            last_err = e
            attempts.append({"via": "direct" if direct else "proxy",
                             "transport": "curl_cffi",
                             "ok": False, "status": None,
                             "error": type(e).__name__,
                             "detail": _safe_err(e, proxy)})
            res = None  # urllib gets a shot below, on every route
        if res is not None:
            status, raw, resp_headers, _final = res
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = None
            attempts.append({"via": "direct" if direct else "proxy",
                             "transport": "curl_cffi",
                             "ok": True, "status": status, "error": None})
            return status, parsed, raw, resp_headers, bool(proxy), attempts
        try:
            status, parsed, raw, resp_headers = _urllib_json_attempt(
                method, url, headers=headers, data=data,
                timeout=attempt_timeout, proxy=proxy)
            attempts.append({"via": "direct" if direct else "proxy",
                             "transport": "urllib",
                             "ok": True, "status": status, "error": None})
            return status, parsed, raw, resp_headers, bool(proxy), attempts
        except Exception as e:
            last_err = e
            attempts.append({"via": "direct" if direct else "proxy",
                             "transport": "urllib",
                             "ok": False, "status": None,
                             "error": type(e).__name__,
                             "detail": _safe_err(e, proxy)})
            continue
    suffix = (" (proxy bhi try hua, phir direct — dono fail)"
              if tried_proxy else "")
    return None, None, "NETWORK: %s: %s%s" % (
        type(last_err).__name__ if last_err else "Timeout",
        last_err or "no time left", suffix), {}, False, attempts


# ---------------------------------------------------------------- HTTP responses

def send_json(handler, status, payload):
    """Send a JSON response from a BaseHTTPRequestHandler."""
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
