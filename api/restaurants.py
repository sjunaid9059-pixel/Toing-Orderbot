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
from _lib import (SWIGGY_BASE, http_json_all_routes, proxy_override_from_headers,
                  query_params, send_json, web_headers, web_headers_desktop)


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


def fetch_all_restaurants(lat, lng, proxy_override=None):
    """Fetch the restaurant list via EVERY route (direct + proxy) AND both
    User-Agents (mobile + desktop) in PARALLEL and merge/dedupe by id.
    ROOT CAUSE, live-probed 2026-11-26: the 9-vs-28 gap is NOT the proxy —
    it is the User-Agent. The backend's mobile UA gets 9 restaurants
    (CakeZone first); a desktop Chrome UA gets 28 (Lucky Restaurant
    first); 7 mobile-only restaurants (bakeries) are missing from the
    desktop set and 21 desktop-only ones are missing from mobile. One
    UA alone hides the other set, so the user only ever saw 9. The union
    of both UAs (across all routes) is the real set (~30+). Pagination
    is dropped: page 2 via pageOffset.nextOffset returns the SAME
    restaurants as page 1 (dedup adds 0). Returns (restaurants,
    routes_note, error_str_or_None). Never invents a restaurant."""
    from concurrent.futures import ThreadPoolExecutor
    url = ("%s/dapi/restaurants/list/v5?lat=%s&lng=%s"
           "&is-seo-homepage-enabled=true&page_type=DESKTOP_WEB_LISTING"
           % (SWIGGY_BASE, lat, lng))
    variants = [("mobile", web_headers()), ("desktop", web_headers_desktop())]

    def _one(variant):
        name, headers = variant
        return name, http_json_all_routes(
            "GET", url, headers=headers,
            timeout=20, proxy_override=proxy_override)

    variant_results = {}
    with ThreadPoolExecutor(max_workers=len(variants)) as ex:
        for name, results in ex.map(_one, variants):
            variant_results[name] = results
    seen = {}
    order = []
    counts = {"direct": 0, "proxy": 0, "mobile": 0, "desktop": 0}
    routes_ok = 0
    blocked = False
    first_status = None
    total_results = 0
    for name, results in variant_results.items():
        for (proxy, status, data, _raw, _h) in results:
            total_results += 1
            if status is None:
                continue
            if first_status is None:
                first_status = status
            if status == 202:
                blocked = True
                continue
            if status != 200 or not isinstance(data, dict) or data.get("statusCode") != 0:
                continue
            routes_ok += 1
            rs = parse_restaurants(data)
            counts["direct" if proxy is None else "proxy"] += len(rs)
            counts[name] += len(rs)
            for r in rs:
                if r["id"] not in seen:
                    seen[r["id"]] = r
                    order.append(r["id"])
    rests = [seen[i] for i in order]
    routes = {"direct": counts["direct"], "proxy": counts["proxy"],
              "mobile": counts["mobile"], "desktop": counts["desktop"],
              "union": len(rests), "routesOk": routes_ok}
    if rests:
        return rests, routes, None
    if blocked:
        return [], routes, "swiggy_blocked"
    if not total_results:
        return [], routes, "Toing server tak request nahi pahunchi"
    return [], routes, "Toing se restaurants nahi mile (HTTP %s)" % first_status


def handle(params, proxy_override=None):
    try:
        lat = float(params.get("lat") or 17.3850)
        lng = float(params.get("lng") or 78.4867)
    except Exception:
        return 400, {"ok": False, "error": "lat/lng sahi nahi hain"}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return 400, {"ok": False, "error": "lat/lng range se bahar hain"}
    rests, routes, err = fetch_all_restaurants(lat, lng, proxy_override=proxy_override)
    if err == "swiggy_blocked":
        return 502, {"ok": False, "error": "swiggy_blocked",
                     "message": "Toing ne is waqt request block kar di — thodi der baad try karein"}
    if err and not rests:
        return 502, {"ok": False, "error": err}
    return 200, {"ok": True, "lat": lat, "lng": lng,
                 "count": len(rests), "pages": routes.get("routesOk", 0),
                 "routes": routes,
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
