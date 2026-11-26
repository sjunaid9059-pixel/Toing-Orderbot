"""GET /api/restaurants?lat&lng -> REAL Swiggy restaurant list.

Calls GET /dapi/restaurants/list/v5 (open endpoint, 200) with browser
headers and returns the real restaurants near the given location.
No hardcoded restaurants anywhere — an empty list means Swiggy returned
none for that location, and that is what the user sees.
"""
import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (SWIGGY_BASE, http_json, proxy_override_from_headers,
                  query_params, send_json, web_headers)


def _paise_to_rs(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) // 100
    m = re.search(r"(\d+)", str(v).replace(",", ""))
    return int(m.group(1)) if m else None


def _paise_to_rs(v):
    """costForTwo/price comes as paise int (50000) but sometimes as a
    string like 'Rs 200 for two' — never let one bad value kill the card."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) // 100
    m = re.search(r"(\d+)", str(v).replace(",", ""))
    return int(m.group(1)) if m else None


def parse_restaurants(data):
    out = []
    try:
        cards = data["data"]["cards"]
    except Exception:
        return out
    for card in cards:
        try:
            inner = card.get("card", {}).get("card", {})
            grid = inner.get("gridElements", {}).get("infoWithStyle", {}).get("restaurants", [])
        except Exception:
            continue
        for entry in grid:
            try:
                info = entry.get("info", {}) or {}
                if not info.get("id"):
                    continue
                sla = info.get("sla") or {}
                fee = info.get("feeDetails") or {}
                disc = info.get("aggregatedDiscountInfoV3") or info.get("aggregatedDiscountInfoV2") or {}
                fee_val = fee.get("totalFee")
                out.append({
                    "id": str(info.get("id")),
                    "name": info.get("name") or "",
                    "areaName": info.get("areaName") or "",
                    "locality": info.get("locality") or "",
                    "cuisines": info.get("cuisines") or [],
                    "avgRating": info.get("avgRating"),
                    "avgRatingString": info.get("avgRatingString") or "",
                    "totalRatingsString": info.get("totalRatingsString") or "",
                    "costForTwo": _paise_to_rs(info.get("costForTwo")),
                    "costForTwoMessage": info.get("costForTwoMessage") or "",
                    "deliveryTime": sla.get("deliveryTime"),
                    "slaString": sla.get("slaString") or "",
                    "distanceKm": sla.get("lastMileTravel"),
                    "serviceability": sla.get("serviceability") or "",
                    "isOpen": bool((info.get("availability") or {}).get("opened", True)),
                    "veg": bool(info.get("veg")) if info.get("veg") is not None else None,
                    "isNewlyOnboarded": bool(info.get("isNewlyOnboarded")),
                    "imageId": info.get("cloudinaryImageId") or "",
                    "deliveryFee": (float(fee_val) / 100.0) if isinstance(fee_val, (int, float)) else None,
                    "discountHeader": disc.get("header") or "",
                    "discountSubHeader": disc.get("subHeader") or "",
                    "discountTag": disc.get("discountTag") or "",
                })
            except Exception:
                continue
    return out


def _next_offset(data):
    """Swiggy's pagination token for the restaurant list, or ''."""
    try:
        return str((data.get("data") or {}).get("pageOffset", {}).get("nextOffset") or "")
    except Exception:
        return ""


def fetch_all_restaurants(lat, lng, max_pages=5, proxy_override=None):
    """Fetch the restaurant list to exhaustion: page 1, then follow
    Swiggy's pageOffset.nextOffset token until no new restaurants appear
    or max_pages is reached. Live probe 2026-11-26: one page returns ~28
    restaurants for Hyderabad and the token chain adds only 0-2 more —
    the ~28 ARE the full serviceable set for that location at that time
    (the window moves with live availability). Returns (restaurants,
    pages_fetched, error_str_or_None). Never invents a restaurant."""
    import urllib.parse
    seen = {}
    order = []
    token = ""
    pages = 0
    last_error = None
    for _ in range(max_pages):
        q = ("lat=%s&lng=%s&is-seo-homepage-enabled=true&page_type=DESKTOP_WEB_LISTING"
             % (lat, lng))
        if token:
            q += "&nextOffset=" + urllib.parse.quote(token)
        url = "%s/dapi/restaurants/list/v5?%s" % (SWIGGY_BASE, q)
        status, data, _raw, _h = http_json("GET", url, headers=web_headers(), timeout=20,
                                          proxy_override=proxy_override)
        pages += 1
        if status is None:
            last_error = "Toing server tak request nahi pahunchi"
            break
        if status == 202:
            last_error = "swiggy_blocked"
            break
        if status != 200 or not isinstance(data, dict) or data.get("statusCode") != 0:
            last_error = "Toing se restaurants nahi mile (HTTP %s)" % status
            break
        new = 0
        for r in parse_restaurants(data):
            if r["id"] not in seen:
                seen[r["id"]] = r
                order.append(r["id"])
                new += 1
        token = _next_offset(data)
        if not token or new == 0:
            break
    return [seen[i] for i in order], pages, last_error


def handle(params, proxy_override=None):
    try:
        lat = float(params.get("lat") or 17.3850)
        lng = float(params.get("lng") or 78.4867)
    except Exception:
        return 400, {"ok": False, "error": "lat/lng sahi nahi hain"}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return 400, {"ok": False, "error": "lat/lng range se bahar hain"}
    rests, pages, err = fetch_all_restaurants(lat, lng, proxy_override=proxy_override)
    if err == "swiggy_blocked":
        return 502, {"ok": False, "error": "swiggy_blocked",
                     "message": "Toing ne is waqt request block kar di — thodi der baad try karein"}
    if err and not rests:
        return 502, {"ok": False, "error": err}
    return 200, {"ok": True, "lat": lat, "lng": lng,
                 "count": len(rests), "pages": pages,
                 "restaurants": rests}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_GET(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(query_params(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_POST(self):
        send_json(self, 405, {"ok": False, "error": "GET use karein: ?lat&lng"})

    def log_message(self, *args):
        pass
