"""Daemon self-restart helper (v0.11.2+).

Some operations change lifespan-scoped state — most notably the
`fleet.role` flag, which controls whether `candidate_publish_loop`,
`operator_discover_loop`, or `fleet_client.run` is spawned. Those
tasks are created once when `make_app` runs; flipping role after
the fact leaves the daemon in the lifespan corresponding to the
OLD role.

Before v0.11.2 the fix was "tell the user to restart manually." JT
hit this during the first real-hardware v0.11.0 walkthrough: setup
wizard picked Operator, but the discovery loop never started
because the daemon booted in standalone mode. The user has no
reason to suspect they need a manual restart, and the missing
loop is silent — no error, just no discovery.

v0.11.2 adds a polkit rule (`52-driveforge-daemon-restart.rules`)
that authorizes the `driveforge` daemon user to call
`systemctl restart driveforge-daemon.service`. This module wraps
the call so callers don't have to know about polkit vs sudo vs
exec.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


def schedule_self_restart(*, delay_s: float = 1.5, reason: str = "role change") -> None:
    """Queue a daemon restart after `delay_s` seconds.

    The delay gives the current HTTP response time to flush before
    uvicorn's worker gets killed — otherwise the client sees a
    connection reset instead of the 303 redirect that confirms the
    wizard save succeeded.

    Fire-and-forget: spawns a daemon thread that runs the restart
    subprocess and exits. systemd's `Restart=on-failure` + a clean
    SIGTERM bring the daemon back under the new lifespan.

    The `reason` string is logged for audit. Not passed to
    systemctl (there's no way to annotate a unit restart).
    """
    def _go() -> None:
        time.sleep(delay_s)
        logger.info("self-restart: triggering daemon restart (reason: %s)", reason)
        try:
            subprocess.run(
                ["systemctl", "restart", "driveforge-daemon"],
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "self-restart: systemctl restart failed (%s). "
                "Manual restart may be required to apply the change.",
                exc,
            )

    threading.Thread(target=_go, daemon=True).start()
