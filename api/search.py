"""GET /api/search?q&lat&lng&tab=dish|restaurant -> REAL Swiggy dish/restaurant search.

Calls GET /dapi/restaurants/search/v3 (open endpoint, 200) with browser
headers. Results live in data.cards[].groupedCard.cardGroupMap.DISH /
.RESTAURANT — the real Swiggy dish cards with price in paise, stock flag
and the restaurant they belong to. Menu (dapi/menu/pl) is WAF-blocked
server-side, so a restaurant's dishes come from this search, filtered by
restaurant id — no invented menu items.
"""
import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (SWIGGY_BASE, http_json_all_routes, proxy_override_from_headers,
                  query_params, send_json, web_headers)


def _paise_to_rs(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) // 100
    m = re.search(r"(\d+)", str(v).replace(",", ""))
    return int(m.group(1)) if m else None


def _price_rs(v):
    try:
        return round(float(v or 0) / 100.0, 2)
    except Exception:
        return 0.0


def _dish_from_card(card):
    c = card.get("card", {}).get("card", {}) or {}
    info = c.get("info") or {}
    if not info.get("id") or not info.get("name"):
        return None
    rest = (c.get("restaurant") or {}).get("info") or {}
    veg = None
    if info.get("isVeg") is not None:
        veg = bool(info.get("isVeg"))
    elif isinstance(info.get("itemAttribute"), dict):
        vc = info["itemAttribute"].get("vegClassifier")
        if vc in ("VEG", "NONVEG"):
            veg = vc == "VEG"
    return {
        "id": str(info.get("id")),
        "name": info.get("name") or "",
        "category": info.get("category") or "",
        "description": info.get("description") or "",
        "price": _price_rs(info.get("price")),
        "inStock": bool(info.get("inStock", 1)),
        "isVeg": veg,
        "imageId": info.get("imageId") or "",
        "restaurant": {
            "id": str(rest.get("id") or ""),
            "name": rest.get("name") or "",
            "areaName": rest.get("areaName") or "",
            "locality": rest.get("locality") or "",
            "address": rest.get("address") or "",
            "avgRating": rest.get("avgRating"),
            "avgRatingString": rest.get("avgRatingString") or "",
            "costForTwo": _paise_to_rs(rest.get("costForTwo")),
            "cuisines": rest.get("cuisines") or [],
            "imageId": rest.get("cloudinaryImageId") or "",
        },
    }


def _restaurant_from_card(card):
    c = card.get("card", {}).get("card", {}) or {}
    info = c.get("info") or {}
    if not info.get("id") or not info.get("name"):
        return None
    sla = info.get("sla") or {}
    return {
        "id": str(info.get("id")),
        "name": info.get("name") or "",
        "areaName": info.get("areaName") or "",
        "locality": info.get("locality") or "",
        "cuisines": info.get("cuisines") or [],
        "avgRating": info.get("avgRating"),
        "avgRatingString": info.get("avgRatingString") or "",
        "costForTwo": _paise_to_rs(info.get("costForTwo")),
        "deliveryTime": sla.get("deliveryTime"),
        "slaString": sla.get("slaString") or "",
        "distanceKm": sla.get("lastMileTravel"),
        "serviceability": sla.get("serviceability") or "",
        "isOpen": bool((info.get("availability") or {}).get("opened", True)),
        "imageId": info.get("cloudinaryImageId") or "",
    }


def handle(params, proxy_override=None):
    q = (params.get("q") or "").strip()
    if not q:
        return 400, {"ok": False, "error": "Search ke liye q likhein"}
    try:
        lat = float(params.get("lat") or 17.3850)
        lng = float(params.get("lng") or 78.4867)
    except Exception:
        return 400, {"ok": False, "error": "lat/lng sahi nahi hain"}
    tab = (params.get("tab") or "dish").strip().lower()
    import urllib.parse
    url = ("%s/dapi/restaurants/search/v3?lat=%s&lng=%s&str=%s"
           "&trackingId=toing-mini&submitAction=ENTER&queryUniqueId=toing-mini-1"
           % (SWIGGY_BASE, lat, lng, urllib.parse.quote(q)))
    # Same union as restaurants: different routes see different live dish
    # sets (search/v3 caps at ~102 cards per query per route), so fetch
    # via every route in parallel and merge/dedupe by id.
    results = http_json_all_routes("GET", url, headers=web_headers(),
                                   timeout=20, proxy_override=proxy_override)
    dishes_by_id, rests_by_id = {}, {}
    blocked = False
    first_status = None
    any_ok = False
    for (_proxy, status, data, _raw, _h) in results:
        if status is None:
            continue
        if first_status is None:
            first_status = status
        if status == 202:
            blocked = True
            continue
        if status != 200 or not isinstance(data, dict) or data.get("statusCode") != 0:
            continue
        any_ok = True
        try:
            for card in data["data"]["cards"]:
                group = (card.get("groupedCard") or {}).get("cardGroupMap") or {}
                for dish_card in (group.get("DISH") or {}).get("cards") or []:
                    d = _dish_from_card(dish_card)
                    if d and d["id"] not in dishes_by_id:
                        dishes_by_id[d["id"]] = d
                for rest_card in (group.get("RESTAURANT") or {}).get("cards") or []:
                    r = _restaurant_from_card(rest_card)
                    if r and r["id"] not in rests_by_id:
                        rests_by_id[r["id"]] = r
        except Exception:
            pass
    if not any_ok:
        if blocked:
            return 502, {"ok": False, "error": "swiggy_blocked",
                         "message": "Toing ne is waqt request block kar di — thodi der baad try karein"}
        if not results:
            return 502, {"ok": False, "error": "Toing server tak request nahi pahunchi"}
        return 502, {"ok": False, "error": "Toing se search results nahi mile (HTTP %s)" % first_status}
    dishes = list(dishes_by_id.values())
    restaurants = list(rests_by_id.values())
    if tab == "restaurant":
        return 200, {"ok": True, "q": q, "restaurants": restaurants,
                     "dishes": dishes, "count": len(restaurants)}
    return 200, {"ok": True, "q": q, "dishes": dishes,
                 "restaurants": restaurants, "count": len(dishes)}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(query_params(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_POST(self):
        send_json(self, 405, {"ok": False, "error": "GET use karein: ?q&lat&lng"})

    def log_message(self, *args):
        pass
