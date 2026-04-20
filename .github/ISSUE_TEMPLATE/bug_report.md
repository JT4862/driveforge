---
name: Bug report
about: Something's broken — help the maintainer reproduce it
title: "[bug] "
labels: bug
---

<!--
Before filing: this is a solo-maintained homelab project. The more of this
template you fill in, the faster the bug gets diagnosed. Empty reports
("it doesn't work") tend to sit indefinitely. See CONTRIBUTING.md for why.
-->

## What happened

<!-- Plain English, 1-3 sentences. What did you expect, what actually happened? -->


## Reproduction

<!-- Ordered steps, as minimal as possible. Example:
1. Start a batch with one 4 TB Seagate ST4000NM0033
2. Watch the dashboard
3. After ~6 hours the drive card shows "failed"
-->

1.
2.
3.

## Environment

- **DriveForge version**: <!-- from Settings → About, or `driveforge --version` -->
- **Install path**: <!-- ISO flash / git clone + install.sh / other -->
- **Server hardware**: <!-- e.g. Dell R720 LFF / NX-3200 / custom -->
- **HBA**: <!-- e.g. PERC H710 crossflashed to 9207-8i IT mode -->
- **Debian version**: <!-- `cat /etc/os-release | grep VERSION=` -->
- **Kernel**: <!-- `uname -r` -->
- **CPU / RAM**: <!-- optional but useful for performance bugs -->

## The drive(s) involved

<!-- Critical for any test-pipeline bug. Copy the lsblk line for each
affected drive. Add model/SN of any drive that misbehaved. -->

```
$ sudo lsblk -o NAME,SERIAL,MODEL,SIZE,TRAN
<paste here>
```

For SMART details on a specific drive:

```
$ sudo smartctl -i /dev/sdX
<paste the Vendor / Product / Transport / Rotation Rate / Form Factor lines>
```

## Daemon journal tail

```
$ sudo journalctl -u driveforge-daemon -n 100 --no-pager
<paste the last few minutes of logs, especially any lines with WARNING / ERROR / Traceback>
```

## DriveForge install log (if install-related)

```
$ sudo tail -80 /var/log/driveforge-install.log
<paste if the bug is in installation, not runtime>
```

## Web UI console / network (if UI-related)

<!-- Browser devtools → Console tab for JS errors, Network tab for failed requests -->

## What I've already tried

<!-- Optional but helpful: "restarted daemon", "checked smartctl directly", etc. -->

## Anything else

<!-- Screenshots, video of LEDs, quirky backplane info, etc. -->
