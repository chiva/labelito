FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:dc6831ca75771711b69e2fcaf47f2b4938bcfd7721daf254c1131791249d000d AS builder

LABEL org.opencontainers.image.title="labelito"
LABEL org.opencontainers.image.description="Self-hosted label printing for Brother QL printers"
LABEL org.opencontainers.image.licenses="GPL-3.0-or-later"
LABEL org.opencontainers.image.source="https://github.com/chiva/labelito"

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev


# Install + normalize the bundled icon collections (FontAwesome/Material/Octicons) into /icons.
# Runs the same script developers run locally, so the on-disk layout matches both environments.
# corepack pins the exact pnpm version (with integrity hash) from package.json's packageManager field.
# Digest-pinned like the builder/runtime stages: this stage runs pnpm and bakes the normalized SVGs
# into the final image, so an unpinned tag could change the toolchain or assets with no source diff.
# Renovate's dockerfile manager bumps the tag and digest together.
# $BUILDPLATFORM keeps this stage on the build host's native arch in multi-arch builds: its SVG
# output is arch-independent, and running pnpm under QEMU would multiply the build time for nothing.
FROM --platform=$BUILDPLATFORM node:24-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS icons
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && corepack enable
WORKDIR /build
# Manifest, lockfile, and pnpm settings first so the install layer caches unless dependencies change.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY scripts/fetch-icons.sh ./scripts/fetch-icons.sh
RUN bash scripts/fetch-icons.sh /icons


# Pinned to the SAME Debian release (trixie) as the uv builder stage above, and by digest:
# the venv is built against the builder's glibc, so builder and runtime must not drift apart.
# A bare `python:3.13-slim` floats to whatever Debian is current, silently diverging from the
# builder on the next Debian release. Renovate's dockerfile manager bumps both tag and digest.
FROM python:3.14-slim-trixie@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

# libcairo2 is the runtime backing cairosvg (icon-collection SVG rasterization).
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-dejavu-core libusb-1.0-0 libcairo2 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app /app
# Bundled collections live OUTSIDE the assets/icons VOLUME so a user's bind-mount can't shadow them.
COPY --from=icons /icons /app/assets/icon-collections
# Bundled example templates + translation catalogs, copied to paths OUTSIDE the /app/templates and
# /app/translations VOLUMEs (same anti-shadowing split as the icon collections above). The loader and
# translator merge these with the user volumes (EXAMPLE_*_DIR below), so a user who bind-mounts an
# empty/own templates or translations dir still gets the shipped examples, image upgrades always ship
# the latest examples, and the DEFAULT_LANGUAGE catalog is always present (no empty-mount boot crash).
# User files win by internal name/language; these are never a runtime volume, so they can't be shadowed.
COPY --from=builder /app/templates /app/examples/templates
COPY --from=builder /app/translations /app/examples/translations
# Empty the /app/templates and /app/translations VOLUME mountpoints: the shipped content now lives
# ONLY under /app/examples/* (copied just above). Docker seeds an anonymous volume — a bare
# `docker run` with no bind mount — from the image content at the mountpoint, so leaving the shipped
# files here would make the loader read them from templates_dir FIRST (is_example=false), shadowing
# the bundled examples and defeating the split (no example styling/customize links, and a saved
# override can't shadow a bundle). Must run BEFORE the VOLUME declaration (Docker discards changes to
# a volume path made after it). A user bind mount overrides this empty dir, so Compose is unaffected.
RUN rm -rf /app/templates /app/translations && mkdir -p /app/templates /app/translations
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Run non-root by default. /app/data is the only path written at runtime (the SQLite history db),
# so it is the only one that must be owned by the runtime user; everything else is read-only.
# Override at deploy time with Compose `user:` — point it at a uid that owns the mounted data dir.
# The chown must precede the VOLUME declaration below: Docker discards changes made to a volume
# path *after* its VOLUME instruction, so chowning later would leave an anonymous-volume data dir
# owned by root and unwritable by the non-root user on a bare `docker run`.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/data \
 && chown app:app /app/data

VOLUME ["/app/templates", "/app/translations", "/app/fonts", "/app/assets/icons", "/app/data"]

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TEMPLATES_DIR=/app/templates \
    EXAMPLE_TEMPLATES_DIR=/app/examples/templates \
    TRANSLATIONS_DIR=/app/translations \
    EXAMPLE_TRANSLATIONS_DIR=/app/examples/translations \
    FONTS_DIR=/app/fonts \
    ICONS_DIR=/app/assets/icons \
    ICON_COLLECTIONS_DIR=/app/assets/icon-collections \
    DATA_DIR=/app/data

USER app

EXPOSE 8765

# /readyz is dependency-aware (templates loaded, transport resolvable, history store open; 503 when
# not serving) and unauthenticated, unlike /health which returns ok unconditionally. It deliberately
# does NOT probe the printer — a transient printer blip must not mark the container unhealthy
# (see the /readyz docstring in app/main.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/readyz')"

# Single worker is intentional — do NOT add --workers N. Print serialization (_print_lock) is an
# in-process asyncio.Lock and retry de-duplication runs against an in-process history store (a
# single SQLite connection); multiple workers would each hold their own and race both, producing
# concurrent sends and duplicate labels. One printer needs one worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
