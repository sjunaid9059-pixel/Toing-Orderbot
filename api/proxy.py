"""GET /api/proxy -> {ok, proxyConfigured}.

Lets the admin check whether TOING_PROXY is set in Vercel without ever
exposing the proxy URL or credentials. Returns only a boolean.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import proxy_configured, send_json


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        send_json(self, 200, {"ok": True, "proxyConfigured": proxy_configured()})

    def do_POST(self):
        send_json(self, 405, {"ok": False, "error": "GET use karein"})

    def log_message(self, *args):
        pass
