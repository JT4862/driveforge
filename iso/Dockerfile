# DriveForge ISO build environment.
#
# Used by scripts/build-iso-docker.sh to build the installer ISO on any host
# with Docker — including macOS, where the native scripts can't run because
# they need apt-get + xorriso. Runs as a Debian 12 container, downloads
# the netinst ISO + apt deps + Python wheels, and produces the final
# bootable ISO into /src/dist/ on the host.
#
# Build with: docker build -t driveforge-iso-builder iso/
# Or via the wrapper: ./scripts/build-iso-docker.sh

FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Tools needed for the build pipeline:
#   xorriso        — repacks the netinst ISO with our preseed + bundle
#   apt-utils      — apt-cache / apt-get download
#   isolinux       — provides isohdpfx.bin for hybrid BIOS boot
#   python3-pip    — pip download for the Python wheel cache
#   git, tar, curl — source archive + ISO download
RUN apt-get update && apt-get install -y --no-install-recommends \
      xorriso \
      apt-utils \
      isolinux \
      python3 python3-pip python3-venv \
      git \
      tar gzip \
      curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Default to invoking the build script; override with `docker run ... bash`
# for interactive debugging.
ENTRYPOINT ["/bin/bash"]
CMD ["./scripts/build-iso.sh"]
