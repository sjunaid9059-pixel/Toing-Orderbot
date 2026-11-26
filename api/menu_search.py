"""GET /api/menu_search?restaurantId&lat&lng&name&cuisines -> dishes of ONE restaurant via real Toing dish search.

menu/pl is WAF-blocked server-side, and searching by restaurant NAME alone
returns 0 dishes for many restaurants (live probe 2026-11-26: 'CakeZone
Patisserie' -> 0 dishes, but 'cake' -> 102 dishes including 2 of CakeZone's).
This endpoint searches several live terms in PARALLEL — the restaurant
name, parts of the name (camelCase split, e.g. CakeZone -> Cake), each
cuisine term, the first word of each cuisine — and keeps ONLY dishes whose
restaurant.id matches the requested id. Every dish comes from a real
search/v3 response; nothing is invented. If no term finds a dish of this
restaurant, count is 0 and the frontend says so honestly.
"""
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (SWIGGY_BASE, http_json, proxy_override_from_headers,
                  query_params, send_json, web_headers)
from search import _dish_from_card

# Generic words in a restaurant name that make useless search terms.
_STOPWORDS = {"restaurant", "restaurants", "cafe", "cafes", "hotel", "hotels",
              "foods", "food", "kitchen", "kitchens", "the", "and", "of",
              "by", "fresh", "house", "point", "express", "bakery"}


def _search_term(q, lat, lng, proxy_override=None):
    """One search/v3 call for one term -> list of dish dicts (any restaurant)."""
    url = ("%s/dapi/restaurants/search/v3?lat=%s&lng=%s&str=%s"
           "&trackingId=toing-mini&submitAction=ENTER&queryUniqueId=toing-mini-1"
           % (SWIGGY_BASE, lat, lng, urllib.parse.quote(q)))
    status, data, _raw, _h = http_json("GET", url, headers=web_headers(),
                                      timeout=15, proxy_override=proxy_override)
    if status != 200 or not isinstance(data, dict) or data.get("statusCode") != 0:
        return []
    out = []
    try:
        for card in data["data"]["cards"]:
            group = (card.get("groupedCard") or {}).get("cardGroupMap") or {}
            for dish_card in (group.get("DISH") or {}).get("cards") or []:
                d = _dish_from_card(dish_card)
                if d:
                    out.append(d)
    except Exception:
        pass
    return out


def _name_parts(name):
    """Searchable words out of a restaurant name: space-split words plus
    camelCase splits ('CakeZone' -> 'Cake', 'Zone'). Generic words dropped."""
    parts = []
    for w in re.split(r"[^A-Za-z]+", name or ""):
        if not w:
            continue
        for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", w):
            if len(p) >= 3 and p.lower() not in _STOPWORDS and p.lower() not in [x.lower() for x in parts]:
                parts.append(p)
    return parts


def _build_terms(name, cuisines):
    """Query terms in priority order, deduped, capped at 6 (parallel calls)."""
    terms = []

    def _add(t):
        t = (t or "").strip()
        if t and t.lower() not in [x.lower() for x in terms] and len(terms) < 6:
            terms.append(t)

    _add(name)
    for p in _name_parts(name)[:2]:
        _add(p)
    for c in (cuisines or [])[:3]:
        _add(c)
        words = (c or "").split()
        if words:
            _add(words[0])
    return terms


def handle(params, proxy_override=None):
    rid = (params.get("restaurantId") or params.get("id") or "").strip()
    if not rid:
        return 400, {"ok": False, "error": "restaurantId dein"}
    try:
        lat = float(params.get("lat") or 17.3850)
        lng = float(params.get("lng") or 78.4867)
    except Exception:
        return 400, {"ok": False, "error": "lat/lng sahi nahi hain"}
    name = (params.get("name") or "").strip()
    cuisines = [c.strip() for c in (params.get("cuisines") or "").split(",") if c.strip()]
    if not name or not cuisines:
        # Look the restaurant up in the live list (union of all routes).
        try:
            from restaurants import fetch_all_restaurants
            rests, _routes, _err = fetch_all_restaurants(
                lat, lng, proxy_override=proxy_override)
            for r in rests:
                if str(r["id"]) == rid:
                    name = name or r.get("name") or ""
                    if not cuisines:
                        cuisines = list(r.get("cuisines") or [])
                    break
        except Exception:
            pass
    terms = _build_terms(name, cuisines)
    if not terms:
        return 200, {"ok": True, "restaurantId": rid, "restaurantName": name,
                     "count": 0, "dishes": [], "queries": []}
    found = {}
    with ThreadPoolExecutor(max_workers=len(terms)) as ex:
        for dishes in ex.map(
                lambda q: _search_term(q, lat, lng, proxy_override), terms):
            for d in dishes:
                try:
                    if str((d.get("restaurant") or {}).get("id")) == rid \
                            and d["id"] not in found:
                        found[d["id"]] = d
                except Exception:
                    continue
    dishes = list(found.values())
    return 200, {"ok": True, "restaurantId": rid, "restaurantName": name,
                 "count": len(dishes), "dishes": dishes, "queries": terms}


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
