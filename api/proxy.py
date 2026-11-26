"""GET /api/proxy -> proxy status, credentials-free.

Lets the admin check which proxy (if any) the backend is using, without
ever exposing the proxy URL or credentials. Returns only:
  proxyConfigured (bool), proxyHost (masked hostname or null),
  proxySource ('request' | 'pool' | 'env' | 'none'),
  overrideRejected (bool — an X-Toing-Proxy header was sent but invalid),
  lastRoute ({transport, via} of the last successful backend call, or {}).

Per-request override: send X-Toing-Proxy: http://user:pass@host:port and
that proxy is used for that request (validated: http/https + host only).
Remove the override and the backend falls back to the server env proxy
(TOING_PROXIES pool / TOING_PROXY), then direct.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (effective_proxy_status, last_route,
                  proxy_override_from_headers, send_json)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        ov, rejected = proxy_override_from_headers(self)
        st = effective_proxy_status(ov, rejected)
        send_json(self, 200, {"ok": True,
                              "proxyConfigured": st["configured"],
                              "proxyHost": st["host"],
                              "proxySource": st["source"],
                              "overrideRejected": st["overrideRejected"],
                              "lastRoute": last_route()})

    def do_POST(self):
        send_json(self, 405, {"ok": False, "error": "GET use karein"})

    def log_message(self, *args):
        pass
