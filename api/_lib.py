"""Shared helpers for the Toing Mini backend (Vercel Python serverless).

Stdlib only — no third-party dependencies, so the Vercel deploy cannot fail
on a missing wheel. Ported from the Telegram bot (~/workspace/toing_bot/toing_bot.py):
same Toing app identity, same profile.swiggy.com auth endpoints, same login JSON
shape, same bill/discount rules. NEVER use Toing app headers on Swiggy dapi/*
endpoints (instant 403) — dapi calls always use browser headers.
"""
import base64
import json
import os
import random
import re
import string
import urllib.error
import urllib.parse
import urllib.request
import uuid

TOING_BASE_URL = "https://profile.swiggy.com/api/v3/app"
SWIGGY_BASE = "https://www.swiggy.com"
TOING_PACKAGE = "in.swiggy.android.toing"

TOING_DEVICE_POOL = [
    {"vendor": "GOOGLE", "hardware": "Pixel 8", "ver": "1103", "sw": "1.12.0"},
    {"vendor": "SAMSUNG", "hardware": "SM-S921B", "ver": "1105", "sw": "1.12.1"},
    {"vendor": "ONEPLUS", "hardware": "CPH2573", "ver": "1101", "sw": "1.11.0"},
    {"vendor": "XIAOMI", "hardware": "2211133G", "ver": "1104", "sw": "1.12.0"},
]

BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")

COUPONS = {
    # BHIMNEW — verified live: flat Rs 50 off on orders above Rs 99 via BHIM UPI,
    # once per user (Swiggy applies Rs 49.99). Sources + user's own screenshot.
    "BHIMNEW": {"off": 49.99, "min": 99, "desc": "BHIMNEW: Rs 50 off on Rs 99+ via BHIM UPI (Toing applies Rs 49.99)"},
    # PAYTMUPI — verified: flat Rs 50 CASHBACK with Paytm UPI / UPI Lite on
    # payments above Rs 100, once across Food/Instamart/Dineout/Toing.
    # Cashback hai — bill se nahi kat-ta, Paytm me wapas milta hai.
    "PAYTMUPI": {"cashback": 50, "min": 100, "desc": "PAYTMUPI: Rs 50 cashback on Paytm UPI, min Rs 100 (cashback, bill se nahi kat-ta)"},
}
# NOTE: TOING50 removed 2026-11-26 — no evidence it is a real Swiggy/Toing
# coupon anywhere; it came from the old demo file. Only verified coupons
# get a local bill discount. Any other code the user types is NOT rejected
# locally — it is passed to Swiggy checkout, which validates it for real.
REFER_CASH = 50.0
REFER_MIN_CART = 100.0
GST_RATE = 0.05  # Swiggy charges 5% GST on the discounted food total

_RANDOM_FIRST = ["Aarav", "Vihaan", "Ananya", "Diya", "Rohan", "Kabir", "Meera",
                 "Arjun", "Sneha", "Vikram", "Neha", "Aditya", "Ishita", "Rahul",
                 "Pooja", "Karan", "Simran", "Nikhil", "Anika", "Dev", "Aisha",
                 "Riya", "Aman", "Tara", "Yash"]
_RANDOM_LAST = ["Sharma", "Patel", "Singh", "Gupta", "Mehta", "Verma", "Iyer",
                "Reddy", "Khan", "Das", "Nair", "Joshi", "Agarwal", "Malhotra",
                "Kapoor", "Chopra", "Bhatt", "Rao", "Menon", "Kulkarni"]


def random_user_name():
    return "%s %s" % (random.choice(_RANDOM_FIRST), random.choice(_RANDOM_LAST))


def new_device_id():
    return "".join(random.choices("0123456789abcdef", k=16))


# ---------------------------------------------------------------- HTTP helpers

def web_headers():
    """Browser headers for Swiggy dapi/* — never Toing headers here."""
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.swiggy.com/",
        "Origin": "https://www.swiggy.com",
    }


def toing_headers(device_id, dev=None):
    """Real Toing Android client identity — profile/auth calls ONLY.
    The device is chosen DETERMINISTICALLY from the deviceId (same phone
    every call): the Telegram bot fixes one device per client so send+verify
    look like the same phone, and the mini backend must do the same —
    a random device per call would make send and verify carry different
    version-code / manufacturer / model headers for one session."""
    if dev is None:
        try:
            idx = int(str(device_id or "")[:8], 16) % len(TOING_DEVICE_POOL)
        except Exception:
            idx = 0
        dev = TOING_DEVICE_POOL[idx]
    ua = "Toing-Android/%s (Android 13; %s)" % (dev["sw"], dev["hardware"])
    return {
        "User-Agent": ua,
        "user-agent": ua,
        "content-type": "application/json; charset=utf-8",
        "accept": "application/json; charset=utf-8",
        "version-code": str(dev["ver"]),
        "app-version": str(dev["sw"]),
        "os-version": "13",
        "platform": "android",
        "manufacturer": dev["vendor"],
        "model-name": dev["hardware"],
        "swuid": device_id,
        "deviceid": device_id,
        "deviceId": device_id,
        "package-name": TOING_PACKAGE,
        "X-Package-Name": TOING_PACKAGE,
        "app-name": "toing",
    }


def _proxy_url():
    """Outbound proxy for Swiggy calls, when the admin set one.
    Env var TOING_PROXY, format http://user:pass@host:port (or
    http://host:port). Unset/empty = direct, exactly as before.
    Never log or return the credential part of the URL."""
    v = (os.environ.get("TOING_PROXY") or "").strip()
    return v or None


def proxy_configured():
    return bool(_proxy_url())


def http_json(method, url, headers=None, body=None, timeout=20):
    """One HTTP call returning (status, parsed_json_or_None, raw_text, resp_headers).

    Never raises for HTTP error statuses. Returns status None only on a
    network failure. `body` is serialised as JSON when given.
    When TOING_PROXY is set, the request goes through that proxy
    (urllib ProxyHandler, auth from the URL); otherwise the default
    opener is used (direct, or the runtime's own egress proxy)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    proxy = _proxy_url()
    opener = None
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        resp = (opener.open(req, timeout=timeout) if opener
                else urllib.request.urlopen(req, timeout=timeout))
        with resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = None
            return resp.status, parsed, raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        return e.code, parsed, raw, dict(e.headers)
    except Exception as e:
        return None, None, "NETWORK: %s: %s" % (type(e).__name__, e), {}


def http_text(method, url, headers=None, timeout=20):
    """Like http_json but returns raw text (for HTML pages) and the final
    URL after redirects. Returns (status, final_url, raw_text, headers)."""
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    proxy = _proxy_url()
    opener = None
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        resp = (opener.open(req, timeout=timeout) if opener
                else urllib.request.urlopen(req, timeout=timeout))
        with resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), raw, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return e.code, e.geturl(), raw, dict(e.headers)
    except Exception as e:
        return None, url, "NETWORK: %s: %s" % (type(e).__name__, e), {}


# ---------------------------------------------------------------- JWT / login JSON

def decode_jwt(t):
    try:
        chunk = str(t or "").split(".")[1]
        pad = chunk + "=" * ((4 - len(chunk) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(pad).decode())
    except Exception:
        return {}


def jwt_user_id(t):
    """customerId out of a JWT (tid/token). '0' is a pre-registration tid
    (expires in ~40 min) and must NEVER become the customerId."""
    d = decode_jwt(t)
    uid = str(d.get("user_id") or "").strip()
    if uid and uid != "0":
        return uid
    m = re.search(r"userId~(\d+)", str(d.get("sub") or ""))
    if m and m.group(1) != "0":
        return m.group(1)
    return ""


def _extract_token_deep(obj, depth=0):
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        for k in ("access_token", "accessToken", "token", "auth_token", "authToken",
                  "session_token", "sessionToken", "jwt", "bearer_token", "bearerToken"):
            v = obj.get(k)
            if isinstance(v, str) and len(v.strip()) >= 8:
                return v.strip()
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _extract_token_deep(v, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for item in obj[:10]:
            if isinstance(item, (dict, list)):
                found = _extract_token_deep(item, depth + 1)
                if found:
                    return found
    return None


def _extract_token_from_headers(headers):
    try:
        h = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:
        return None
    for k in ("token", "access-token", "x-access-token", "x-auth-token", "authorization"):
        v = (h.get(k) or "").strip()
        if v.lower().startswith("bearer "):
            v = v[7:].strip()
        if len(v) >= 8:
            return v
    return None


def build_toing_login_json(mobile10, verify_body):
    """Merge top-level + data + user + customer, then fill customerId/sid
    from the tid/token JWTs when the body omits them."""
    data = verify_body if isinstance(verify_body, dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    user = inner.get("user") if isinstance(inner.get("user"), dict) else {}
    customer = inner.get("customer") if isinstance(inner.get("customer"), dict) else {}
    merged = {**data, **inner, **user, **customer}

    def _pick(*keys):
        for k in keys:
            v = merged.get(k)
            if v not in (None, "", 0, "0"):
                return str(v)
        return ""

    token = _pick("token", "accessToken", "access_token", "authToken", "session_token", "jwt")
    tid = _pick("tid", "trackingId", "tracking_id")
    sid = _pick("sid", "sessionId", "session_id")
    cid = _pick("customerId", "customer_id", "userId", "user_id", "userid", "id")
    if cid == "0":
        cid = ""
    if not cid and tid:
        cid = jwt_user_id(tid)
    if not cid and token:
        cid = jwt_user_id(token)
    if not sid and tid:
        sid = str(decode_jwt(tid).get("sid") or "")
    return {
        "mobile": mobile10,
        "name": _pick("name", "fullName", "full_name", "customerName"),
        "token": token,
        "access_token": token,
        "sid": sid,
        "tid": tid,
        "deviceId": _pick("deviceId", "device_id", "deviceID"),
        "customerId": cid,
        "_raw": verify_body,
    }


def public_login_json(acc):
    """The exact 7-field login JSON, in field order:
    mobile, name, token, sid, tid, deviceId, customerId.
    Long-lived: when the server sends no separate token, the tid JWT
    itself becomes the token (~30 days). customerId '0' is never returned."""
    token = str(acc.get("token") or acc.get("access_token") or "")
    tid = str(acc.get("tid") or "")
    if not token and tid:
        token = tid
    cid = str(acc.get("customerId") or acc.get("customer_id")
              or acc.get("userId") or acc.get("user_id") or acc.get("id") or "")
    if cid == "0":
        cid = ""
    if not cid and tid:
        cid = jwt_user_id(tid)
    if not cid and token:
        cid = jwt_user_id(token)
    return {
        "mobile": str(acc.get("mobile") or acc.get("phone") or ""),
        "name": str(acc.get("name") or ""),
        "token": token,
        "sid": str(acc.get("sid") or ""),
        "tid": tid,
        "deviceId": str(acc.get("deviceId") or acc.get("device_id") or ""),
        "customerId": cid,
    }


# ---------------------------------------------------------------- Bill / discounts

def calc_bill(item_total, coupon=None, refer_balance=REFER_CASH):
    """Exact discount logic (verified against the reference flow):
    BHIMNEW -49.99 on cart >= 99 (flat Rs 50 off above Rs 99 via BHIM UPI);
    PAYTMUPI = Rs 50 cashback on Paytm UPI, min Rs 100 (NOT a bill discount);
    refer cash 50 only on cart >= 100 (new accounts); GST 5% on the
    discounted total. Unknown coupon codes are NOT rejected here — Swiggy
    checkout validates them for real, so the bill carries a note instead.
    Delivery fee is NOT invented here — the caller fills it from Swiggy's
    feeDetails when available, otherwise it stays None (confirmed at
    browser checkout)."""
    item_total = round(float(item_total or 0), 2)
    coupon = (coupon or "").strip().upper()
    coupon_off = 0.0
    coupon_msg = ""
    cashback = 0.0
    if coupon in COUPONS:
        rule = COUPONS[coupon]
        if item_total >= rule["min"]:
            if "off" in rule:
                coupon_off = rule["off"]
            elif "cashback" in rule:
                cashback = rule["cashback"]
                coupon_msg = "Rs %s cashback Paytm me milega (bill se nahi kat-ta)" % _inr(cashback)
        else:
            coupon_msg = "%s ke liye Rs %s aur add karein (min Rs %s)" % (
                coupon, _inr(rule["min"] - item_total), rule["min"])
    elif coupon:
        coupon_msg = "Coupon Toing checkout pe validate hoga — wahan se asli discount lagega"
    refer_off = 0.0
    refer_msg = ""
    if item_total >= REFER_MIN_CART and float(refer_balance or 0) > 0:
        refer_off = min(REFER_CASH, float(refer_balance))
    elif 0 < item_total < REFER_MIN_CART:
        refer_msg = "Refer Cash Rs 50 sirf Rs 100+ cart pe lagta hai"
    discounted = round(item_total - coupon_off - refer_off, 2)
    gst = round(max(0.0, discounted) * GST_RATE, 2)
    return {
        "itemTotal": item_total,
        "coupon": coupon or None,
        "couponOff": round(coupon_off, 2),
        "couponMsg": coupon_msg,
        "cashback": round(cashback, 2),
        "referOff": round(refer_off, 2),
        "referMsg": refer_msg,
        "gst": gst,
        "deliveryFee": None,
        "deliveryFeePending": True,
        "toPay": round(max(0.0, discounted + gst), 2),
    }


def _inr(n):
    n = round(float(n), 2)
    return ("%g" % n) if n == int(n) else ("%.2f" % n)


# ---------------------------------------------------------------- Vercel handler plumbing

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization, tid, sid, deviceId",
        "Content-Type": "application/json",
    }


def send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    for k, v in cors_headers().items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except Exception:
        length = 0
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def query_params(handler):
    parsed = urllib.parse.urlparse(handler.path)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def valid_mobile(mobile):
    m = str(mobile or "").strip()
    m = re.sub(r"^\+?91", "", m) if m.startswith("+91") or (len(m) == 12 and m.startswith("91")) else m
    m = m.lstrip("0")
    return m if re.fullmatch(r"[6-9]\d{9}", m) else None


def valid_login(login):
    """A login JSON is usable only with a real (non-'0') customerId and a token/tid."""
    if not isinstance(login, dict):
        return False, "Login JSON missing hai"
    pub = public_login_json(login)
    if not pub["customerId"]:
        return False, "Ye sirf registration session hai (customerId '0') — dobara OTP verify karke full login JSON lein"
    if not pub["token"] and not pub["tid"]:
        return False, "Login JSON me token/tid missing hai"
    if not pub["mobile"]:
        return False, "Login JSON me mobile missing hai"
    return True, pub


# ---------------------------------------------------------------- Address resolving (real, never guessed)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"


def reverse_geocode(lat, lng):
    """lat/lng -> real formatted address via OpenStreetMap Nominatim.
    Returns (True, {lat, lng, text, city, area, road, postcode}) or
    (False, {error}). No invented address, ever."""
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:
        return False, {"error": "lat/lng sahi nahi hain"}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False, {"error": "lat/lng range se bahar hain"}
    url = ("%s?format=json&lat=%s&lon=%s&zoom=18&addressdetails=1"
           % (NOMINATIM_URL, lat, lng))
    status, data, _raw, _h = http_json(
        "GET", url,
        headers={"User-Agent": "ToingMini/1.0 (toing-mini-order-bot)",
                 "Accept": "application/json"}, timeout=15)
    if status != 200 or not isinstance(data, dict) or not data.get("display_name"):
        return False, {"error": "Address resolve nahi hua (Nominatim HTTP %s)" % status}
    addr = data.get("address") or {}
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("county") or addr.get("state_district") or "")
    area = (addr.get("suburb") or addr.get("neighbourhood")
            or addr.get("residential") or "")
    return True, {"lat": lat, "lng": lng, "text": data.get("display_name") or "",
                  "city": city, "area": area, "road": addr.get("road") or "",
                  "postcode": addr.get("postcode") or ""}


_LATLNG_RES = [
    re.compile(r"[?&]lat=(-?\d+\.\d+)[^&]{0,60}?[&;]lng=(-?\d+\.\d+)"),
    re.compile(r"[?&]lng=(-?\d+\.\d+)[^&]{0,60}?[&;]lat=(-?\d+\.\d+)"),
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)(?:,|$)"),
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),
    re.compile(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)"),
    re.compile(r'"latitude"\s*:\s*(-?\d+\.\d+)[^}]{0,120}?"longitude"\s*:\s*(-?\d+\.\d+)'),
    re.compile(r'"lat"\s*:\s*(-?\d+\.\d+)\s*,\s*"lng"\s*:\s*(-?\d+\.\d+)'),
]


def _latlng_from_text(text):
    """First plausible lat/lng pair in a URL or HTML body, or (None, None)."""
    if not text:
        return None, None
    for rx in _LATLNG_RES:
        m = rx.search(text)
        if m:
            try:
                a, b = float(m.group(1)), float(m.group(2))
            except Exception:
                continue
            # !3d/!4d and @lat,lng are (lat, lng); the lat=/lng= patterns
            # are already ordered; the lng-first pattern swaps them back.
            if rx.pattern.startswith("[?&]lng="):
                a, b = b, a
            if -90 <= a <= 90 and -180 <= b <= 180 and a != 0 and b != 0:
                return a, b
    return None, None


def resolve_shared_link(url):
    """Follow a pasted share link FOR REAL and resolve it to an address.

    Follows redirects server-side, then looks for real coordinates in the
    final URL and in the fetched page (embedded JSON). When coordinates are
    found they are reverse-geocoded to a real formatted address. When no
    coordinates are found, returns a truthful error — the city is NEVER
    guessed from the link text. Returns (True, {...}) or (False, {error})."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False, {"error": "Sahi link paste karein (http:// ya https:// se shuru hona chahiye)"}
    lat, lng = _latlng_from_text(url)
    status, final_url, raw, _h = http_text(
        "GET", url, headers=web_headers(), timeout=20)
    if status is None:
        return False, {"error": "Link khul nahi paya: %s" % (raw or "")[:120]}
    if lat is None:
        lat, lng = _latlng_from_text(final_url or "")
    if lat is None:
        lat, lng = _latlng_from_text(raw or "")
    if lat is None:
        return False, {"error": "Is link se location nahi mili — link me lat/lng nahi hai. Address manually add karein ya current location use karein"}
    ok, geo = reverse_geocode(lat, lng)
    if not ok:
        return False, geo
    geo["sourceUrl"] = final_url or url
    return True, geo
