"""POST /api/orders {login} -> REAL order history from Swiggy.

Calls GET /dapi/order/all with browser headers and the real session
cookies (tid/token/_sid from the login JSON). Swiggy answers 200 with
statusCode 1 'Session expired' when the session is dead — that is
surfaced honestly, never papered over with sample orders. When the
session is alive, the real orders Swiggy returns are listed.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (SWIGGY_BASE, http_json, proxy_override_from_headers,
                  read_json_body, send_json, valid_login, web_headers)


def _norm_order(o):
    if not isinstance(o, dict):
        return None
    rest = o.get("restaurant") or o.get("restaurantInfo") or {}
    if isinstance(rest, dict):
        rest_name = rest.get("name") or ""
    else:
        rest_name = str(rest or "")
    total = o.get("orderTotal") or o.get("total") or o.get("amount") or 0
    try:
        total = round(float(total) / 100.0, 2) if float(total) > 1000 else round(float(total), 2)
    except Exception:
        total = 0
    items = o.get("items") or o.get("orderItems") or []
    if isinstance(items, list):
        items_txt = ", ".join(
            "%s x%s" % (str((i or {}).get("name") or ""), (i or {}).get("quantity") or 1)
            for i in items[:8] if isinstance(i, dict))
    else:
        items_txt = str(items or "")
    return {
        "id": str(o.get("orderId") or o.get("order_id") or o.get("id") or ""),
        "restaurant": rest_name,
        "items": items_txt,
        "total": total,
        "status": str(o.get("status") or o.get("orderStatus") or ""),
        "when": str(o.get("orderTime") or o.get("createdAt") or o.get("time") or ""),
        "payment": str(o.get("paymentMethod") or o.get("payment_method") or ""),
    }


def handle(body, proxy_override=None):
    login = body.get("login") or body
    ok, pub_or_err = valid_login(login)
    if not ok:
        return 400, {"ok": False, "error": pub_or_err}
    pub = pub_or_err
    headers = web_headers()
    cookies = []
    if pub["tid"]:
        cookies.append("tid=%s" % pub["tid"])
    if pub["token"]:
        cookies.append("token=%s" % pub["token"])
    if pub["sid"]:
        cookies.append("_sid=%s" % pub["sid"])
    if pub["deviceId"]:
        cookies.append("_device_id=%s" % pub["deviceId"])
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    headers["tid"] = pub["tid"]
    headers["sid"] = pub["sid"]
    status, data, _raw, _h = http_json(
        "GET", "%s/dapi/order/all" % SWIGGY_BASE, headers=headers, timeout=20,
        proxy_override=proxy_override)
    if status is None:
        return 502, {"ok": False, "error": "Toing server tak request nahi pahunchi"}
    if status == 202:
        return 502, {"ok": False, "error": "swiggy_blocked",
                     "message": "Toing ne is waqt request block kar di — thodi der baad try karein"}
    if status != 200 or not isinstance(data, dict):
        return 502, {"ok": False, "error": "Toing se orders nahi mile (HTTP %s)" % status}
    if data.get("statusCode") not in (None, 0):
        return 401, {"ok": False, "error": "session_expired",
                     "message": str(data.get("statusMessage") or data.get("message")
                                    or "Session expire ho gaya — dobara login karein")}
    raw_orders = None
    for path in (("data", "orders"), ("data", "orderList"), ("orders",), ("data", "data", "orders")):
        cur = data
        try:
            for k in path:
                cur = cur[k]
            if isinstance(cur, list):
                raw_orders = cur
                break
        except Exception:
            continue
    orders = []
    for o in (raw_orders or []):
        n = _norm_order(o)
        if n and n["id"]:
            orders.append(n)
    return 200, {"ok": True, "count": len(orders), "orders": orders}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_POST(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(read_json_body(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_GET(self):
        send_json(self, 405, {"ok": False, "error": "POST use karein: {login}"})

    def log_message(self, *args):
        pass
