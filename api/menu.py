"""GET /api/menu?restaurantId&lat&lng -> REAL Swiggy menu (when Swiggy allows it).

Calls GET /dapi/menu/pl with browser headers. HONEST LIMITATION: this
endpoint is behind Swiggy's AWS WAF JavaScript challenge server-side — it
returns 202 empty with x-amzn-waf-action: challenge from any HTTP client,
even with warmed cookies. This endpoint therefore returns a truthful
`menu_blocked` error instead of fake items; the frontend gets the
restaurant's dishes from /api/search (real dish results), never from here.
If Swiggy ever stops challenging this endpoint, this handler parses the
real menu and returns it.
"""
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (SWIGGY_BASE, http_json, proxy_override_from_headers,
                  query_params, send_json, web_headers)


def _parse_menu(data):
    items = []
    try:
        for card in data["data"]["cards"]:
            grouped = card.get("groupedCard", {}).get("cardGroupMap", {})
            for _tab, group in grouped.items():
                for sub in (group or {}).get("cards") or []:
                    inner = sub.get("card", {}).get("card", {}) or {}
                    for ic in inner.get("itemCards") or []:
                        info = ((ic.get("card") or {}).get("info")) or {}
                        if info.get("id") and info.get("name"):
                            try:
                                price = round(float(info.get("price") or 0) / 100.0, 2)
                            except Exception:
                                price = 0.0
                            # Addons: Swiggy menu info carries addons as
                            # [{groupId, groupName, choices: [{id, name, price, inStock}],
                            #  maxAddons, minAddons, ...}] — parsed verbatim,
                            # never invented; empty when the API sends none.
                            addons = []
                            for g in (info.get("addons") or []):
                                if not isinstance(g, dict):
                                    continue
                                choices = []
                                for ch in (g.get("choices") or []):
                                    if isinstance(ch, dict) and ch.get("id") and ch.get("name"):
                                        try:
                                            cprice = round(float(ch.get("price") or 0) / 100.0, 2)
                                        except Exception:
                                            cprice = 0.0
                                        choices.append({"id": str(ch.get("id")), "name": ch.get("name") or "",
                                                        "price": cprice, "inStock": bool(ch.get("inStock", 1))})
                                if choices:
                                    addons.append({"groupId": str(g.get("groupId") or ""),
                                                   "groupName": g.get("groupName") or "",
                                                   "maxAddons": g.get("maxAddons"),
                                                   "minAddons": g.get("minAddons"),
                                                   "choices": choices})
                            items.append({
                                "id": str(info.get("id")),
                                "name": info.get("name") or "",
                                "category": info.get("category") or "",
                                "description": info.get("description") or "",
                                "price": price,
                                "inStock": bool(info.get("inStock", 1)),
                                "isVeg": bool(info.get("isVeg")) if info.get("isVeg") is not None else None,
                                "imageId": info.get("imageId") or "",
                                "addons": addons,
                            })
    except Exception:
        pass
    return items


def handle(params, proxy_override=None):
    rid = (params.get("restaurantId") or params.get("id") or "").strip()
    if not rid:
        return 400, {"ok": False, "error": "restaurantId dein"}
    try:
        lat = float(params.get("lat") or 17.3850)
        lng = float(params.get("lng") or 78.4867)
    except Exception:
        return 400, {"ok": False, "error": "lat/lng sahi nahi hain"}
    url = ("%s/dapi/menu/pl?page-type=REGULAR&complete-menu=true&lat=%s&lng=%s"
           "&restaurantId=%s&catalog_qa=undefined&submitAction=ENTER"
           % (SWIGGY_BASE, lat, lng, urllib.parse.quote(rid)))
    status, data, _raw, _h = http_json("GET", url, headers=web_headers(), timeout=20,
                                      proxy_override=proxy_override)
    if status is None:
        return 502, {"ok": False, "error": "Toing server tak request nahi pahunchi"}
    if status == 202:
        return 502, {"ok": False, "error": "menu_blocked",
                     "message": "Toing ka menu endpoint server-side security check ke peeche hai — "
                                "is restaurant ke dishes Search se dikhaye jayenge (real Toing dish results)"}
    if status != 200 or not isinstance(data, dict) or data.get("statusCode") != 0:
        return 502, {"ok": False, "error": "Toing se menu nahi mila (HTTP %s)" % status}
    items = _parse_menu(data)
    return 200, {"ok": True, "restaurantId": rid, "count": len(items), "items": items}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(query_params(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_POST(self):
        send_json(self, 405, {"ok": False, "error": "GET use karein: ?restaurantId&lat&lng"})

    def log_message(self, *args):
        pass
