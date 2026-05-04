# DriveForge Field-Check Live ISO build environment.
#
# Used by scripts/build-fieldcheck-iso-docker.sh to build the Field-Check
# Live ISO on any host with Docker. The Live ISO is a tmpfs-backed Debian
# image that boots into the field_check daemon role — for plug-into-an-
# unknown-server-and-inspect workflows where we MUST NOT touch the host's
# disks.
#
# Distinct from the installer ISO (built via iso/Dockerfile + scripts/
# build-iso.sh which produces a debian-installer remaster). This image
# carries `live-build` instead of debian-installer tooling.
#
# Build with: docker build -t driveforge-fieldcheck-iso-builder iso-fieldcheck/
# Or via the wrapper: ./scripts/build-fieldcheck-iso-docker.sh

FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# live-build is the standard Debian tool for producing live-bootable
# (non-installing) ISOs. The same tooling powers Kali Linux + Tails.
# - debootstrap: bootstraps the Debian root filesystem live-build
#   populates the squashfs from
# - xorriso: lays out the final hybrid-bootable ISO9660 image
# - rsync: live-build uses it for staging includes.chroot
# - syslinux-common + isolinux: BIOS boot loader bits live-build pulls
# - mtools + dosfstools: build the EFI System Partition for UEFI boot
RUN apt-get update && apt-get install -y --no-install-recommends \
      live-build \
      debootstrap \
      xorriso \
      rsync \
      syslinux-common \
      isolinux \
      mtools \
      dosfstools \
      ca-certificates \
      curl

WORKDIR /src
ENTRYPOINT ["/bin/bash"]
CMD ["./scripts/build-fieldcheck-iso.sh"]
