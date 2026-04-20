#!/bin/bash
# Generate /etc/issue with the DriveForge dashboard URL + current IP.
#
# Runs on each boot via driveforge-issue.service. The banner shows above
# the TTY login prompt so an operator walking up to the console knows
# exactly where to point their browser — Proxmox-style.
#
# The IP is resolved fresh each boot because DHCP-assigned IPs can
# change across reboots and we want the banner to reflect reality.

set -euo pipefail

# Primary IPv4 — same detection logic install.sh uses for its final
# "access URLs" summary. Falls back through a couple of sources so we
# still print something useful even on weird network configs.
PRIMARY_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1); exit}')
PRIMARY_IP=${PRIMARY_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}
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
