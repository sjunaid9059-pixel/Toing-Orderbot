# Proxy for the Toing Mini order bot

The backend (Vercel Python serverless functions in `api/`) talks to the
Toing/Swiggy platform APIs. All of those calls go through one helper,
`http_json` / `http_text` in `api/_lib.py`, and honour an outbound proxy
when one is configured.

## Env var (only one thing to set)

```
TOING_PROXY=http://user:pass@host:port
```

- Format: a normal HTTP(S) proxy URL. `user:pass@` is optional if the
  proxy needs no auth. `https://` URLs also work.
- When `TOING_PROXY` is **unset or empty**, all calls go direct — the
  app works exactly as before.
- When it **is set**, every Swiggy-platform call (restaurants, search,
  suggest, menu, orders, OTP send/verify, checkout_prepare) is routed
  through that proxy. The frontend's browser-context checkout (Telegram
  WebView on the user's phone) does **not** use this proxy — it runs on
  the phone's own network, which is how the WAF challenge is passed.

## Where to set it

**Vercel (production):** Vercel dashboard → project `toing-orderbot` →
Settings → Environment Variables → add `TOING_PROXY` with the proxy URL
→ Redeploy (Deployments → ⋯ → Redeploy). The function reads the env var
at request time, so a redeploy after adding it is enough.

**Local test:** `export TOING_PROXY=http://user:pass@host:port` before
running the functions, or set it in the shell that starts the dev server.

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
  only as the `TOING_PROXY` env var in Vercel / your local shell.
- If a proxy stops working, unset the var and redeploy — the app falls
  back to direct calls automatically.
- `GET /api/proxy` (same file set) reports only whether a proxy is
  configured (`proxyConfigured: true/false`) — it never returns the URL.
