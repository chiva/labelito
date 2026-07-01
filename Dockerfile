FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:d8a45a30929a5bfedd8b09d630538ca1ab30041154d2a6cb2e4fee3cffd3ea4c AS builder

LABEL org.opencontainers.image.title="Labelito"
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
FROM node:24-slim@sha256:b31e7a42fdf8b8aa5f5ed477c72d694301273f1069c5a2f71d53c6482e99a2fc AS icons
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
FROM python:3.13-slim-trixie@sha256:2b7445fb71ca9cb15e9aab053fe8cb3162796f8e1d92ada12a49c766a811bc1e

# libcairo2 is the runtime backing cairosvg (icon-collection SVG rasterization).
RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-dejavu-core libusb-1.0-0 libcairo2 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app /app
# Bundled collections live OUTSIDE the assets/icons VOLUME so a user's bind-mount can't shadow them.
COPY --from=icons /icons /app/assets/icon-collections
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
    TRANSLATIONS_DIR=/app/translations \
    FONTS_DIR=/app/fonts \
    ICONS_DIR=/app/assets/icons \
    ICON_COLLECTIONS_DIR=/app/assets/icon-collections \
    DATA_DIR=/app/data

USER app

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/health')"

# Single worker is intentional — do NOT add --workers N. Print serialization (_print_lock) is an
# in-process asyncio.Lock and retry de-duplication runs against an in-process history store (a
# single SQLite connection); multiple workers would each hold their own and race both, producing
# concurrent sends and duplicate labels. One printer needs one worker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
