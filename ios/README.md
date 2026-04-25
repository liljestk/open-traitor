# OpenTraitor iOS

Native SwiftUI app that talks to your existing FastAPI dashboard over Tailscale.
No new backend, no new auth — uses the same `/api/*` endpoints and `ot_session`
cookie that the web dashboard uses.

## Architecture

```
iPhone (Tailscale on)  ──tailnet──►  your-host.tailnet-name.ts.net:8090
       SwiftUI app                   src/dashboard/server.py (FastAPI)
       URLSession + HTTPCookieStorage
       Keychain (server URL, password optional)
```

* **Auth:** `POST /api/auth/login {password}` → cookie persisted by
  `URLSession.shared` automatically. CSRF token fetched from
  `GET /api/auth/status` and attached to mutating requests.
* **Domain separation:** every request appends `?profile=coinbase` or
  `?profile=ibkr` (selected in Settings) — same contract as the web UI.
* **Transport:** plain HTTP over Tailscale is fine inside your tailnet.
  If you set `DASHBOARD_HTTPS=1`, switch the URL to `https://...`.

## Build (requires a Mac with Xcode 15+)

```bash
brew install xcodegen
cd ios
xcodegen generate
open OpenTraitor.xcodeproj
# Pick your phone as the run target → Cmd+R
```

For sideloading on your personal device, set the team in
`project.yml` → `DEVELOPMENT_TEAM` (or override in Xcode → Signing).

## First launch

1. Open the app → **Settings** tab.
2. Server URL: `http://<your-host>.<tailnet>.ts.net:8090`
   (find via `tailscale status` on the host running the dashboard).
3. Profile: `coinbase` or `ibkr`.
4. Tap **Save & Login**, enter your dashboard password.

## What's included

| Tab        | Endpoints used                                                         |
|------------|------------------------------------------------------------------------|
| Overview   | `/stats/summary`, `/portfolio/exposure`, `/portfolio/history`          |
| Trades     | `/trades?hours=168`, `/trades/sync` (POST)                             |
| Cycles     | `/cycles`, `/cycles/{id}`                                              |
| Predictions| `/predictions/accuracy`, `/predictions/tracked-pairs`                  |
| Settings   | `/auth/status`, `/auth/login`, `/auth/logout`, `/settings` (read-only) |

Live WebSocket monitor (`/ws`) is intentionally **not** included in v1 —
poll every 10s instead. Add later if needed.

## Tailscale tips

* Enable **MagicDNS** in the Tailscale admin so `http://hostname:8090` works.
* Add the host to the iPhone's Tailscale acl group; no port-forwarding needed.
* If the dashboard is behind `nginx` on the host, point the URL at `:443/443`
  with the appropriate scheme.
