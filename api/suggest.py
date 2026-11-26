"""GET /api/suggest?q&lat&lng -> REAL Swiggy search suggestions.

Calls GET /dapi/restaurants/search/suggest (open endpoint, 200) with
browser headers. Returns the real autocomplete suggestions Swiggy gives
for the typed query.
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import SWIGGY_BASE, http_json, query_params, send_json, web_headers


def handle(params):
    q = (params.get("q") or "").strip()
    if not q:
        return 400, {"ok": False, "error": "q likhein"}
    try:
        lat = float(params.get("lat") or 17.3850)
        lng = float(params.get("lng") or 78.4867)
    except Exception:
        return 400, {"ok": False, "error": "lat/lng sahi nahi hain"}
    url = ("%s/dapi/restaurants/search/suggest?lat=%s&lng=%s&str=%s&trackingId=toing-mini"
           % (SWIGGY_BASE, lat, lng, urllib.parse.quote(q)))
    status, data, _raw, _h = http_json("GET", url, headers=web_headers(), timeout=20)
    if status is None:
        return 502, {"ok": False, "error": "Swiggy tak request nahi pahunchi"}
    if status == 202:
        return 502, {"ok": False, "error": "swiggy_blocked",
                     "message": "Swiggy ne is waqt request block kar di (WAF challenge)"}
    if status != 200 or not isinstance(data, dict) or data.get("statusCode") != 0:
        return 502, {"ok": False, "error": "Swiggy se suggestions nahi mile (HTTP %s)" % status}
    out = []
    try:
        for s in (data.get("data") or {}).get("suggestions") or []:
            out.append({
                "text": s.get("text") or "",
                "type": s.get("type") or "",
                "tag": s.get("tagToDisplay") or "",
                "metadata": s.get("metadata") or "",
                "imageId": s.get("cloudinaryId") or "",
            })
    except Exception:
        pass
    return 200, {"ok": True, "q": q, "suggestions": out}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        status, payload = handle(query_params(self))
        send_json(self, status, payload)

    def do_POST(self):
        send_json(self, 405, {"ok": False, "error": "GET use karein: ?q&lat&lng"})

    def log_message(self, *args):
        pass
