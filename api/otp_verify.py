"""POST /api/otp_verify  {mobile, otp, tid, sid, deviceId} -> real Toing login.

Calls the REAL POST /login/verify with cloningSignalsData and the session
headers from otp_send. A wrong OTP fails with the server's own message —
no OTP is ever accepted locally. Single OTP creates a new account: when
verify returns a pre-registration tid (customerId '0'), we try /signup
with a random name (session headers on every call). If signup still cannot
produce a real customerId, we automatically send a FRESH OTP (round 2, the
same fix the Telegram bot's Firebase flow uses) and ask the user for the
second OTP — a customerId '0' session is never saved or returned as a login.
"""
import os
import re
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (TOING_BASE_URL, TOING_PACKAGE, _extract_token_deep,
                  _extract_token_from_headers, build_toing_login_json,
                  http_json, jwt_user_id, proxy_override_from_headers,
                  public_login_json, random_user_name, read_json_body,
                  send_json, toing_headers, valid_mobile)


def _verify_once(mobile, otp, tid, sid, device_id, proxy_override=None):
    """One real verify call. Returns (ok, login_dict_or_None, error_str, raw_data)."""
    headers = toing_headers(device_id)
    headers["tid"] = tid or ""
    headers["sid"] = sid or ""
    headers["deviceId"] = device_id
    headers["token"] = ""
    body = {
        "cloningSignalsData": {
            "appFilesDirPathInvalid": 0,
            "developerModeEnabled": 0,
            "deviceModelVmos": 0,
            "emulatorStatus": 0,
            "packageName": TOING_PACKAGE,
            "workProfileEnabled": 0,
        },
        "otp": otp,
    }
    url = "%s/login/verify?otp_source=Sms-automatic" % TOING_BASE_URL
    status, data, raw, resp_headers = http_json("POST", url, headers=headers, body=body, timeout=20,
                                               proxy_override=proxy_override)
    if status is None:
        return False, None, "Toing auth server tak request nahi pahunchi", None
    if status not in (200, 201):
        return False, None, "Toing auth server ne HTTP %s diya" % status, None
    if not isinstance(data, dict):
        return False, None, "Toing se ajeeb response aaya (non-JSON)", None
    login = build_toing_login_json(mobile, data)
    token = login.get("token") or _extract_token_deep(data) or _extract_token_from_headers(resp_headers)
    code = data.get("statusCode")
    msg = str(data.get("statusMessage") or data.get("message") or "")
    if not token:
        err = msg or "No token in response"
        if code is not None:
            err = "%s (code %s)" % (err, code)
        return False, None, err, data
    login["token"] = token
    login["access_token"] = token
    return True, login, "", data


def _complete_new_account(mobile, login, device_id, proxy_override=None):
    """Try /signup with a random name to turn a pre-registration session
    into a real account. Returns (ok, login). Proven: /signup is the only
    endpoint that exists for this; it validates the name strictly."""
    name = random_user_name()
    headers = toing_headers(device_id)
    headers["tid"] = str(login.get("tid") or "")
    headers["sid"] = str(login.get("sid") or "")
    headers["deviceId"] = str(login.get("deviceId") or device_id)
    headers["token"] = ""
    cur = dict(login)
    for body in ({"name": name}, {"name": name, "mobile": mobile}, {"name": name, "email": ""}):
        status, data, _raw, _h = http_json(
            "POST", "%s/signup" % TOING_BASE_URL, headers=headers, body=body, timeout=10,
            proxy_override=proxy_override)
        if status not in (200, 201, 204) or not isinstance(data, dict):
            continue
        msg = str(data.get("statusMessage") or data.get("message") or "")
        if data.get("statusCode") not in (None, 0):
            if "name" not in msg.lower():
                break
            continue
        # signup accepted — harvest any new token/tid/customerId
        tok = _extract_token_deep(data)
        if tok:
            cur["token"] = tok
            cur["access_token"] = tok
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        for k in ("tid", "sid", "customerId", "customer_id", "userId", "user_id"):
            v = data.get(k) or inner.get(k)
            if v and str(v) not in ("0", ""):
                cur[k if k != "user_id" else "customerId"] = str(v)
        if not cur.get("customerId") and cur.get("tid"):
            cur["customerId"] = jwt_user_id(cur["tid"])
        if cur.get("token") and str(cur.get("customerId") or "") not in ("", "0"):
            cur["name"] = name
            return True, cur
        if "name" not in msg.lower():
            break
    if cur.get("token") and str(cur.get("customerId") or "") not in ("", "0"):
        cur["name"] = cur.get("name") or name
        return True, cur
    return False, cur


def _send_fresh_otp(mobile, device_id, proxy_override=None):
    url = "%s/sms_otp?mobile=%s" % (TOING_BASE_URL, mobile)
    status, data, _raw, _h = http_json("GET", url, headers=toing_headers(device_id), timeout=20,
                                      proxy_override=proxy_override)
    if status is None or not isinstance(data, dict) or data.get("statusCode") != 0:
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return {
        "tid": str(data.get("tid") or inner.get("tid") or ""),
        "sid": str(data.get("sid") or inner.get("sid") or ""),
        "deviceId": device_id,
    }


def handle(body, proxy_override=None):
    mobile = valid_mobile(body.get("mobile"))
    otp = str(body.get("otp") or "").strip()
    tid = str(body.get("tid") or "")
    sid = str(body.get("sid") or "")
    device_id = str(body.get("deviceId") or "")
    if not mobile:
        return 400, {"ok": False, "error": "Sahi 10-digit mobile number daalein"}
    if not re.fullmatch(r"\d{4,8}", otp):
        return 400, {"ok": False, "error": "Sahi OTP daalein (4-8 digit)"}
    if not tid or not device_id:
        return 400, {"ok": False,
                     "error": "OTP session expire ho gaya — dobara OTP bhejein"}
    ok, login, err, _data = _verify_once(mobile, otp, tid, sid, device_id, proxy_override)
    if not ok:
        return 400, {"ok": False, "error": "OTP verify nahi hua: %s" % err}
    pub = public_login_json(login)
    if pub["customerId"]:
        pub["name"] = pub["name"] or login.get("name") or ""
        return 200, {"ok": True, "registered": True, "login": pub,
                     "message": "Login ho gaya — account save ho gaya"}
    # Pre-registration session (customerId '0'): try to complete the account.
    ok2, login2 = _complete_new_account(mobile, login, device_id, proxy_override)
    if ok2:
        pub2 = public_login_json(login2)
        if pub2["customerId"]:
            return 200, {"ok": True, "registered": True, "login": pub2,
                         "message": "Naya account ban gaya — login ho gaya"}
    # Signup did not produce a real customerId: automatic round 2, exactly
    # like the Telegram bot's Firebase flow — fresh OTP, verify again.
    fresh = _send_fresh_otp(mobile, device_id, proxy_override)
    if fresh:
        return 200, {"ok": False, "round2": True,
                     "tid": fresh["tid"], "sid": fresh["sid"],
                     "deviceId": fresh["deviceId"], "mobile": mobile,
                     "error": "Pehla OTP sirf number register kar paya — naya OTP bhej diya gaya hai, doosra OTP daalein"}
    return 400, {"ok": False,
                 "error": "Account create nahi hua (customerId 0) — dobara OTP bhejkar try karein"}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_POST(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(read_json_body(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_GET(self):
        send_json(self, 405, {"ok": False, "error": "POST use karein: {mobile, otp, tid, sid, deviceId}"})

    def log_message(self, *args):
        pass
