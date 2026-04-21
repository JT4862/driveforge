---
title: REST API
---

# REST API

> **Stub.** Full per-endpoint reference lands in v0.4.0.

This page will cover, with request/response examples for each:

## Read endpoints (GET)

- `/api/health` — daemon health + active drive serials list
- `/api/drives` — full discovered drive inventory
- `/api/drives/{serial}/telemetry` — temperature + chassis-power time-series for a drive
- `/_partials/bays` — server-rendered HTML partial for HTMX dashboard polling
- `/_partials/update-log` — server-rendered HTML partial for live update-log streaming (v0.3.1+)

## Action endpoints (POST)

- `/batches/new` — start a new batch on selected drives
- `/abort-all` — global abort (all in-flight drives)
- `/drives/{serial}/abort` — per-drive abort (v0.2.2+)
- `/drives/{serial}/identify` — toggle the identify-LED strobe (v0.2.9+)
- `/settings/auto-enroll` — change auto-enroll mode
- `/settings/hostname` — rename the host (v0.2.8+)
- `/settings/install-update` — trigger one-click in-app update (v0.3.1+)
- `/settings/check-updates` — manually trigger a GitHub Releases check
- `/settings/grading` — save grading thresholds
- `/settings/printer` — save printer config
- `/settings/integrations` — save webhook + Cloudflare tunnel
- `/settings/daemon` — save bind host/port (requires daemon restart)
- `/settings/wizard-replay` — re-run the first-run setup wizard

For now, the canonical reference is the FastAPI auto-generated OpenAPI
schema at `http://<your-driveforge>:8080/openapi.json` (or browse the
interactive Swagger UI at `/docs` if FastAPI's docs route is enabled).
