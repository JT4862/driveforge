# Contributing to DriveForge

DriveForge is a **personal homelab project**, developed as a solo effort and
shared publicly under MIT so other homelabbers and small refurbishers can
use it if it fits their workflow. It is not an open community project, and
the scope and direction are set by the maintainer based on the hardware and
use cases they personally test against.

## What this means in practice

### Bug reports — welcome

If something breaks on your hardware or you hit a clear defect, please do
[open an issue](https://github.com/JT4862/driveforge/issues/new?template=bug_report.md).
A good bug report is much more valuable than a PR that tries to fix
something before the symptom is understood. The bug-report template asks
for the specific information (drive model, transport, `lsblk`, journal
tail) that makes most DriveForge bugs diagnosable in minutes instead of
hours.

### Pull requests — not actively accepted

PRs are not the primary contribution path for this project. Specifically:

- **Code PRs** from outside the maintainer will usually be closed with
  thanks but not merged. That isn't a quality judgement — it's about the
  real cost of reviewing, hardware-testing, and long-term-maintaining
  somebody else's code on a solo-maintained drive-destructive tool.
- If you've found a **specific, self-contained fix** for a bug you filed
  first and want to attach a patch, note that in the issue and it'll get
  considered on the merits. No promises.
- **Documentation PRs** (typos, broken links, factual corrections) are
  the easiest kind to accept and are welcome.

### Forks — encouraged

MIT license. Fork it, modify it, run it. If your fork ends up going in a
direction that serves a different audience (e.g. large-scale refurbisher,
different hardware platform, different distro), that's a great outcome —
divergent forks are sometimes the right answer for software this
hardware-specific.

### Feature requests — discussion, not promises

Feel free to open an issue labeled as a feature request, but the
maintainer builds what they need for their own rig. Requests outside that
scope might stay open for years or get closed politely. Forking is
usually the faster path for features that aren't on the roadmap.

## If you want to run DriveForge on your hardware

You don't need to contribute anything — just use it. See:

- [INSTALL.md](INSTALL.md) — getting DriveForge onto your hardware
- [UPDATE.md](UPDATE.md) — keeping it current once installed
- [README.md](README.md) — what DriveForge does and what it looks like
- [BUILD.md](BUILD.md) — architecture + design decisions

## Security issues

If you find a security vulnerability, **please do not open a public issue**.
See [SECURITY.md](SECURITY.md) for the disclosure path.
