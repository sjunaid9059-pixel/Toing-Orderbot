# Proxy for the Toing Mini order bot

The backend (Vercel Python serverless functions in `api/`) talks to the
Toing/Swiggy platform APIs. All of those calls go through one helper,
`http_json` / `http_text` in `api/_lib.py`, and honour an outbound proxy
when one is configured. Traffic uses curl_cffi (Chrome 124 browser
impersonation) with a stdlib urllib fallback — the proxy works for both.

## Env vars (server side — Vercel)

```
TOING_PROXY=http://user:pass@host:port
TOING_PROXIES=http://user:pass@host1:port,http://user:pass@host2:port
```

- `TOING_PROXY` — single default proxy. Format: a normal HTTP(S) proxy
  URL. `user:pass@` is optional if the proxy needs no auth. `https://`
  URLs also work.
- `TOING_PROXIES` — comma-separated pool. On a failure the backend
  rotates to the next proxy in the pool (max 3 per request), then falls
  back to direct.
- When **both are unset/empty**, all calls go direct — the app works
  exactly as before.
- When set, every Swiggy-platform call (restaurants, search, suggest,
  menu, orders, OTP send/verify, checkout_prepare, geocode) is routed
  through the proxy. The frontend's browser-context checkout (Telegram
  WebView on the user's phone) does **not** use this proxy — it runs on
  the phone's own network, which is how the WAF challenge is passed.

## Proxy failure fallback (automatic, no redeploy needed)

If a request through a proxy fails with a connection/timeout error, the
backend retries the next candidate and finally retries **once direct
(no proxy)** before returning an error — a dead or slow proxy never
takes the backend down. Proxy attempts are capped at 8s each and the
whole call at its normal timeout, so a hanging proxy cannot eat the
serverless time budget. HTTP error statuses (202 WAF challenge, 400,
401...) are real responses and are returned at once, never retried.

## Change / add / remove without a redeploy

**From the mini app (Tools → Proxy):** the admin can add, change or
remove a per-device proxy. It is saved in the browser's localStorage
and sent as the `X-Toing-Proxy` header on every API call — it takes
effect immediately, no redeploy. Remove clears it and the backend falls
back to the server env proxy, then direct.

**Per-request header (any client):** send `X-Toing-Proxy:
http://user:pass@host:port` with an API request. The backend uses that
proxy for that request only. The value must be a well-formed http(s)
URL with a host — anything else is rejected and ignored.

## Where to set the server env vars

**Vercel (production):** Vercel dashboard → project `toing-orderbot` →
Settings → Environment Variables → add `TOING_PROXY` / `TOING_PROXIES`
→ Redeploy (Deployments → ⋯ → Redeploy). The function reads the env
vars at request time.

**Local test:** `export TOING_PROXY=http://user:pass@host:port` (or
`TOING_PROXIES=...`) before running the functions.

## Check the current status

`GET /api/proxy` returns only safe info — never the URL or credentials:

```json
{"ok": true, "proxyConfigured": true, "proxyHost": "gw.proxyrise.com",
 "proxySource": "env", "overrideRejected": false, "lastRoute": {"transport": "curl_cffi", "via": "proxy"}}
```

- `proxyHost` is the masked hostname only (no user/pass/port).
- `proxySource` is `request` (X-Toing-Proxy header) | `pool`
  (TOING_PROXIES) | `env` (TOING_PROXY) | `none`.
- `overrideRejected` is true when an X-Toing-Proxy header was sent
  but was not a valid http(s) URL with a host.
- `lastRoute` shows how the last successful backend call went
  (`curl_cffi`/`urllib`, via `proxy`/`direct`).

## What it is for

The Swiggy platform puts its stateful endpoints (`/dapi/menu/pl`,
`/dapi/cart`, `/dapi/cart/checkout`) behind an AWS WAF JavaScript
challenge that no server-side HTTP client can solve — that is why the
final checkout runs in the phone's browser context. A residential /
datacenter proxy can help the server-side read endpoints (restaurants,
search) look like normal traffic and can be required by the admin's
checkout setup. The proxy itself never sees any user password or OTP —
only the same API calls the backend already makes.

## Security

- **Never commit a proxy URL (or any credential) to the repo.** Set it
  only as env vars in Vercel / your local shell, or as the per-device
  override in the mini app (stored only in that browser's localStorage).
- If a proxy stops working, remove the override in Tools, or unset the
  env var and redeploy — the app falls back to direct calls.
- `GET /api/proxy` never returns the proxy URL, username or password —
  only the masked hostname.
