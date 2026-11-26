"""POST /api/otp_send  {mobile} -> real Toing OTP via profile.swiggy.com.

Calls the REAL GET /sms_otp endpoint with the real Toing Android identity.
A wrong/invalid number is rejected by OUR validation; the server's own
verdict (e.g. rate-limit statusCode 9) is surfaced, never hidden.
Returns the session tid/sid/deviceId the verify step must send back —
these are session identifiers, not the login token.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))
from _lib import (TOING_BASE_URL, http_json, new_device_id,
                  proxy_override_from_headers, read_json_body, send_json,
                  toing_headers, valid_mobile)


def handle(body, proxy_override=None):
    mobile = valid_mobile(body.get("mobile"))
    if not mobile:
        return 400, {"ok": False,
                     "error": "Sahi 10-digit mobile number daalein (6-9 se shuru hona chahiye)"}
    device_id = new_device_id()
    url = "%s/sms_otp?mobile=%s" % (TOING_BASE_URL, mobile)
    status, data, raw, _hdrs = http_json("GET", url, headers=toing_headers(device_id), timeout=20,
                                        proxy_override=proxy_override)
    if status is None:
        return 502, {"ok": False,
                     "error": "Toing auth server tak request nahi pahunchi: %s" % raw[:120]}
    if not isinstance(data, dict):
        return 502, {"ok": False,
                     "error": "Toing auth server se ajeeb response aaya (HTTP %s)" % status}
    if data.get("statusCode") != 0:
        return 400, {"ok": False,
                     "error": str(data.get("statusMessage") or data.get("message")
                                  or "OTP bhejne me fail (code %s)" % data.get("statusCode"))}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    tid = str(data.get("tid") or inner.get("tid") or "")
    sid = str(data.get("sid") or inner.get("sid") or "")
    return 200, {"ok": True, "message": "OTP bhej diya gaya +91 %s pe" % mobile,
                 "mobile": mobile, "tid": tid, "sid": sid, "deviceId": device_id}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_json(self, 200, {"ok": True})

    def do_POST(self):
        ov, _rej = proxy_override_from_headers(self)
        status, payload = handle(read_json_body(self), proxy_override=ov)
        send_json(self, status, payload)

    def do_GET(self):
        send_json(self, 405, {"ok": False, "error": "POST use karein: {mobile}"})

    def log_message(self, *args):
        pass
