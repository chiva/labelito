# Dev container image. Base matches production (python:3.13 on Debian trixie — same
# glibc, same Python), full variant so common build tooling and headers are present.
FROM python:3.13-trixie@sha256:54e7879ce487f34f8ee85fc37e7547e843a2358bbcb19ecc273869e9d9fcc7dc

ARG YQ_VERSION=v4.48.1
ARG NODE_MAJOR=24

# uv from the official distroless image — same source the production builder stage uses.
COPY --from=ghcr.io/astral-sh/uv:latest@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /uvx /usr/local/bin/

# CLI tool belt for day-to-day development, plus the system libraries labelito needs
# (mirroring the production Dockerfile):
#   libcairo2        — backs cairosvg (icon-collection SVG rasterization)
#   libusb-1.0-0     — pyusb backend for the USB transport
#   fonts-dejavu-core— faithful label previews (same font the prod image ships)
#   snmp             — snmpget/snmpwalk, for debugging the network printer's SNMP status
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      bash \
      build-essential \
      ca-certificates \
      cmake \
      curl \
      dnsutils \
      fd-find \
      file \
      git \
      git-lfs \
      gnupg \
      iproute2 \
      iputils-ping \
      jq \
      less \
      lsof \
      nano \
      netcat-openbsd \
      openssh-client \
      pkg-config \
      procps \
      ripgrep \
      rsync \
      socat \
      sqlite3 \
      strace \
      sudo \
      tree \
      unzip \
      vim \
      wget \
      xz-utils \
      zip \
      libcairo2 \
      libusb-1.0-0 \
      fonts-dejavu-core \
      snmp \
    && wget -qO /usr/local/bin/yq \
         https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_amd64 \
    && chmod +x /usr/local/bin/yq \
    && rm -rf /var/lib/apt/lists/*

# Node (corepack/pnpm) so scripts/fetch-icons.sh works in-container. Same major as the
# production icons stage (node:24). corepack picks up the pnpm version pinned in
# package.json `packageManager` on first use.
RUN wget -qO- https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && corepack enable \
    && npm install -g \
         npm-check-updates \
         @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI from the official apt repo. Installed here (not via the
# devcontainer github-cli feature) so `gh` is present under plain
# `docker/podman compose` too, not only under the VS Code devcontainer flow.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
