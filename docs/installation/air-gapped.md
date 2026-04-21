---
title: Air-gapped install
---

# Air-gapped install

For environments where the target host has no internet access — SCIF
classified deployments, industrial control systems, hardened OT
networks, or just paranoid homelab setups that don't trust outbound
HTTPS from the storage rig.

The pattern: build a self-contained tarball on a connected host that
contains DriveForge source + every apt deb + every Python wheel
needed, transfer the tarball to the target, point `install.sh` at it
via an env var.

## 1. Build the offline bundle (on a connected host)

Run on any internet-connected Debian 12 box (your laptop with a
Debian VM, a build server, etc.):

```bash
git clone https://github.com/JT4862/driveforge.git
cd driveforge
sudo ./scripts/build-offline-bundle.sh
```

Output lands at `dist/driveforge-offline-X.Y.Z.tar.gz` (~150–250 MB
depending on dependency tree size).

The script:

1. `git archive`s the source tree (clean, no `.git` or untracked
   files)
2. Resolves every transitive apt dependency for the package list in
   `install.sh` and downloads them as `.deb` files into `debs/`
3. Resolves every transitive Python wheel dependency for DriveForge
   and downloads them into `wheels/`
4. Tars + gzips the lot

**Important:** build the bundle on the **same Debian version** as
your target. Bundles built on Debian 11 won't install cleanly on
Debian 12 and vice versa — apt dependency hashes won't match.

## 2. Transfer to the target

Whatever transport works for your environment:

- **USB stick** — most common. The bundle is well under any modern
  USB capacity.
- **Internal apt mirror** — if you have one, drop the bundle on a
  shared NFS mount accessible from the target.
- **Sneakernet** — burn a CD-R if you must.

## 3. Install on the target

Untar, set the env var, run install.sh:

```bash
tar xzf driveforge-offline-X.Y.Z.tar.gz
cd driveforge-offline-X.Y.Z
sudo DRIVEFORGE_OFFLINE_BUNDLE="$(pwd)" ./scripts/install.sh
```

The env var tells `install.sh` to:

- Point `apt` at `file://${DRIVEFORGE_OFFLINE_BUNDLE}/debs` instead of
  Debian's mirrors (writes `/etc/apt/sources.list.d/driveforge-offline.list`)
- Use `pip install --no-index --find-links wheels/` so pip never
  reaches out to PyPI

Everything else in `install.sh` proceeds normally — systemd units,
sudoers rule, daemon enable + start.

## 4. Verify

Same as a connected install:

```bash
systemctl status driveforge-daemon
curl http://localhost:8080/api/health
```

The dashboard works identically. The only thing that won't work is
the **Check for updates** button in **Settings → About / Updates** —
that hits `api.github.com` and will fail with a network error on an
air-gapped host. Settings page renders cleanly anyway; you just
won't see release-availability info.

## 5. Updating an air-gapped install

There's no in-app update path on air-gapped hosts (the daemon can't
reach GitHub). The flow is:

1. On a connected host: rebuild the bundle from a newer DriveForge
   tag.
2. Transfer the new bundle to the target.
3. Untar, set `DRIVEFORGE_OFFLINE_BUNDLE`, re-run `install.sh`.

`install.sh` is idempotent and safe to re-run — it preserves
`/etc/driveforge/*.yaml`, the SQLite DB at `/var/lib/driveforge/`,
and any printed labels / reports.

## What's in the bundle

```
driveforge-offline-X.Y.Z/
├── scripts/
│   ├── install.sh
│   └── (other DriveForge scripts)
├── driveforge/                       # source tree
├── pyproject.toml
├── debs/
│   ├── Packages.gz                   # apt index file
│   ├── smartmontools_*.deb
│   ├── hdparm_*.deb
│   ├── sg3-utils_*.deb
│   ├── nvme-cli_*.deb
│   ├── ipmitool_*.deb
│   ├── ledmon_*.deb
│   └── ...                           # 80-150 packages typically
└── wheels/
    ├── fastapi-*.whl
    ├── uvicorn-*.whl
    ├── sqlalchemy-*.whl
    ├── pydantic-*.whl
    └── ...                           # 30-50 wheels typically
```

## Troubleshooting

### "Unable to locate package" during `apt-get install`

Means the bundle is missing a transitive dep — usually because it was
built on a different Debian point release than your target. Rebuild
the bundle on a host that matches your target's `apt-get update`
state.

### `pip install` errors with "no matching distribution"

Same root cause: bundle Python version mismatch. Rebuild on the
same Python minor version as the target (Debian 12 ships 3.11).

### "Permission denied" reading the bundle

Some sites mount transfer media noexec. Either copy the bundle to a
writable + executable location (`/tmp`, `/root`, `/opt`) before
extracting, or remount the transfer media with `exec`.

## Next steps

Once installed, everything works the same as a connected install:

- [Dashboard tour](../operations/dashboard-tour.md)
- [Auto-enroll](../operations/auto-enroll.md)
- [Hostname rename](../operations/hostname-rename.md) — particularly
  useful on multi-box air-gapped fleets
