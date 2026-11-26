"""POST /api/checkout_prepare {login, address, cart, coupon} -> validated bill.

The server ONLY prepares checkout: it validates the real login, the cart
(single restaurant, real dish prices from Swiggy search), the coupon rules
and the serviceability, and returns the exact bill + the checkout payload
the frontend must send to Swiggy in the real browser context (Telegram
WebView on the user's phone). The server never places an order and never
produces a QR — the official UPI QR comes only from Swiggy's own
/dapi/cart/checkout response in that browser context.

Serviceability check (real, data-driven): when the address has lat/lng,
we fetch Swiggy's live restaurant list AT that location — if the cart's
restaurant is in it, it delivers there; if not, it is out of area.
Without address coordinates the result is 'unknown' and the final check
happens in Swiggy's own checkout.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (calc_bill, proxy_override_from_headers, read_json_body,
                  send_json, valid_login, valid_mobile)
from restaurants import fetch_all_restaurants


def _serviceability(restaurant_id, address, proxy_override=None):
    lat, lng = address.get("lat"), address.get("lng")
    if lat is None or lng is None:
        return ("unknown", "Address ki location (lat/lng) set nahi hai — map pin ya current location se set karein; final delivery check Toing checkout me hoga", None)
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:
        return "unknown", "Address ki lat/lng sahi nahi hain", None
    rests, _pages, err = fetch_all_restaurants(lat, lng, proxy_override=proxy_override)
    if err or not rests:
        return "unknown", "Toing se delivery area check nahi ho paya — final check Toing checkout me hoga", None
    match = next((r for r in rests if r["id"] == str(restaurant_id)), None)
    if match:
        return "serviceable", "", match
    return "out_of_area", ("Ye address us restaurant ki delivery area se bahar hai — "
                           "%s aapke address ke aas-paas deliver nahi karta" % restaurant_id), None


def handle(body, proxy_override=None):
    login = body.get("login") or {}
    ok, pub_or_err = valid_login(login)
    if not ok:
        return 400, {"ok": False, "error": pub_or_err}
    pub = pub_or_err
    address = body.get("address") or {}
    if not isinstance(address, dict) or not (address.get("text") or "").strip():
        return 400, {"ok": False, "error": "Delivery address dein"}
    # Receiver contact — delivery partner isi number pe call karta hai.
    # Bina receiver name + 10-digit mobile ke checkout aage nahi badhta.
    receiver_name = (address.get("receiverName") or "").strip()
    receiver_mobile = (address.get("receiverMobile") or "").strip()
    if not receiver_name:
        return 400, {"ok": False, "error": "Receiver ka naam dein — delivery partner ko chahiye"}
    if not valid_mobile(receiver_mobile):
        return 400, {"ok": False, "error": "Receiver ka sahi 10-digit mobile number dein"}
    cart = body.get("cart") or []
    if not isinstance(cart, list) or not cart:
        return 400, {"ok": False, "error": "Cart khaali hai"}
    rest_ids = {str(i.get("restaurantId") or "") for i in cart if isinstance(i, dict)}
    if len(rest_ids) != 1 or "" in rest_ids:
        return 400, {"ok": False, "error": "Cart me ek hi restaurant ke items hone chahiye"}
    restaurant_id = rest_ids.pop()
    items = []
    item_total = 0.0
    for i in cart:
        try:
            price = float(i.get("price") or 0)
            qty = int(i.get("qty") or 0)
        except Exception:
            return 400, {"ok": False, "error": "Cart item ka price/qty sahi nahi hai"}
        if price <= 0 or qty < 1 or qty > 20:
            return 400, {"ok": False, "error": "Cart item ka price/qty sahi nahi hai"}
        if not (i.get("dishId") and i.get("name")):
            return 400, {"ok": False, "error": "Cart item incomplete hai"}
        items.append({"menu_item_id": str(i["dishId"]), "name": str(i["name"]),
                      "quantity": qty, "price": round(price, 2)})
        item_total += round(price * qty, 2)
    coupon = (body.get("coupon") or "").strip().upper()
    try:
        refer_balance = float(body.get("referBalance") or 50)
    except Exception:
        refer_balance = 50.0
    bill = calc_bill(item_total, coupon, refer_balance)
    serviceable, svc_msg, match = _serviceability(restaurant_id, address, proxy_override)
    if serviceable == "out_of_area":
        return 200, {"ok": True, "serviceable": "out_of_area",
                     "message": svc_msg, "bill": None, "checkoutPayload": None}
    # Delivery fee: Swiggy's own feeDetails for this restaurant at the
    # delivery location, taken from the serviceability match — never invented.
    delivery_fee = None
    if match and match.get("deliveryFee") is not None:
        delivery_fee = match["deliveryFee"]
    if delivery_fee is not None:
        bill["deliveryFee"] = round(delivery_fee, 2)
        bill["deliveryFeePending"] = False
        bill["toPay"] = round(max(0.0, item_total - bill["couponOff"] - bill["referOff"]
                                  + delivery_fee + bill["gst"]), 2)
    delivery_contact = {"name": receiver_name, "mobile": receiver_mobile}
    payload = {
        "restaurant_id": restaurant_id,
        "items": items,
        "address": {"text": address.get("text") or "", "city": address.get("city") or "",
                    "label": address.get("label") or "Home",
                    "lat": address.get("lat"), "lng": address.get("lng")},
        "deliveryContact": delivery_contact,
        "coupon": bill["coupon"] or "",
        "payment_method": "UPI",
        "amount": bill["toPay"],
        "amount_paise": int(round(bill["toPay"] * 100)),
    }
    return 200, {"ok": True, "serviceable": serviceable,
                 "serviceableNote": svc_msg if serviceable == "unknown" else "",
                 "bill": bill, "restaurantId": restaurant_id,
                 "deliveryContact": delivery_contact,
                 "checkoutPayload": payload}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_POST(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(read_json_body(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_GET(self):
        send_json(self, 405, {"ok": False, "error": "POST use karein: {login, address, cart, coupon}"})

    def log_message(self, *args):
        pass
