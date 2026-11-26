"""POST /api/geocode  {op, ...} -> real address resolving.

op=reverse {lat, lng}: browser geolocation -> real formatted address via
  OpenStreetMap Nominatim. The user confirms the resolved address before
  saving — nothing is saved silently.
op=resolve {url}: follow a pasted share link FOR REAL (redirects + page),
  extract real coordinates from the final URL / page, reverse-geocode
  them. When the link carries no coordinates the error says so plainly —
  the city is NEVER guessed from link text.

No address, city or coordinate is ever invented here.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (proxy_override_from_headers, read_json_body,
                  resolve_shared_link, reverse_geocode, send_json)


def handle(body, proxy_override=None):
    op = (body.get("op") or "").strip().lower()
    if op == "reverse":
        ok, out = reverse_geocode(body.get("lat"), body.get("lng"),
                                  proxy_override=proxy_override)
        if not ok:
            return 400, {"ok": False, "error": out.get("error") or "Address resolve nahi hua"}
        return 200, {"ok": True, "op": "reverse", **out}
    if op == "resolve":
        ok, out = resolve_shared_link(body.get("url") or body.get("link") or "",
                                      proxy_override=proxy_override)
        if not ok:
            return 400, {"ok": False, "error": out.get("error") or "Link resolve nahi hua"}
        return 200, {"ok": True, "op": "resolve", **out}
    return 400, {"ok": False, "error": "op dein: reverse ya resolve"}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_POST(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(read_json_body(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_GET(self):
        send_json(self, 405, {"ok": False, "error": "POST use karein: {op, ...}"})

    def log_message(self, *args):
        pass
