#!/bin/bash
# Generate /etc/issue with the DriveForge dashboard URL + current IP.
#
# Runs on each boot via driveforge-issue.service. The banner shows above
# the TTY login prompt so an operator walking up to the console knows
# exactly where to point their browser — Proxmox-style.
#
# The IP is resolved fresh each boot because DHCP-assigned IPs can
# change across reboots and we want the banner to reflect reality.

# set -e is INTENTIONALLY omitted here: the whole script is built on
# "try a detection method; if it fails, fall through to the next; if
# they all fail, use a placeholder." With set -e + pipefail, a failed
# `ip route get` on a host without a default route would abort the
# script with exit=2 (which systemctl then surfaces as failed state)
# even though the banner writes correctly from the fallback path. set
# -u is kept so typos still blow up fast.
set -uo pipefail

# Primary IPv4 — same detection logic install.sh uses for its final
# "access URLs" summary. Falls back through a couple of sources so we
# still print something useful even on weird network configs. Each
# step is `|| echo ""` so a non-zero from the upstream tool can't
# propagate into the surrounding shell state.
PRIMARY_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1); exit}' || echo "")
PRIMARY_IP=${PRIMARY_IP:-$(hostname -I 2>/dev/null | awk '{print $1}' || echo "")}
PRIMARY_IP=${PRIMARY_IP:-<no-ip>}

# getty(8) honors a set of backslash escapes in /etc/issue — we use:
#   \n = hostname (set to `driveforge` by the installer preseed)
#   \r = OS release (kernel version)
# Everything else is literal.

cat > /etc/issue <<EOF

  DriveForge on \\n (kernel \\r)

  Dashboard:
    → http://driveforge.local:8080     (preferred — mDNS)
    → http://${PRIMARY_IP}:8080          (direct IP)

  Admin SSH:
    → ssh forge@driveforge.local
    → ssh forge@${PRIMARY_IP}

EOF

# Explicit success exit — guards against any accidental non-zero
# intermediate exit code leaking into the script's final status. The
# cat heredoc above is the only load-bearing command; everything prior
# is best-effort IP sniffing whose failure is benign.
exit 0
