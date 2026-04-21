---
title: Aborting drives
---

# Aborting drives

> **Stub.** Full walkthrough lands in v0.4.0.

This page will cover:

- The per-drive Abort button on every Active card (v0.2.2+)
- Why Abort is disabled during `secure_erase` — the SAS sg_format mid-flight is unsafe to interrupt
- Aborting a drive marks the run with `phase="aborted"` and `grade=None` — treated as "untested" everywhere (v0.2.6+)
- Re-inserting an aborted drive WILL trigger auto-enroll (the abort wasn't a verdict)
- The global `POST /abort-all` endpoint (curl-only, no UI button) for emergency stop-everything
- What aborting actually does subprocess-side: SIGTERM, 3s grace, SIGKILL via `kill_owner(serial)`
