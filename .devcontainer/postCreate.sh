#!/usr/bin/env bash
set -euo pipefail

# Configure git from the GitHub CLI instead of mounting the host ~/.gitconfig.
# Authenticate once with `gh auth login` (the token persists on the gh-config
# volume, see docker-compose.yml); this runs on every container create, so the
# config is re-applied after rebuilds.
if command -v gh >/dev/null 2>&1; then
  # Route GitHub auth through the container's gh (from PATH).
  git config --global 'credential.https://github.com.helper' '!gh auth git-credential'

  # Derive the commit identity from the authenticated account. GitHub keeps the
  # real email private, so use its noreply address. Requires gh to be logged in,
  # so this is skipped on the very first create (before `gh auth login`) and
  # applied automatically on later creates once the token is persisted.
  if gh auth status >/dev/null 2>&1; then
    login=$(gh api user -q .login 2>/dev/null || true)
    id=$(gh api user -q .id 2>/dev/null || true)
    name=$(gh api user -q '.name // .login' 2>/dev/null || true)
    if [ -n "$login" ] && [ -n "$id" ]; then
      git config --global user.name "$name"
      git config --global user.email "${id}+${login}@users.noreply.github.com"
    fi
  fi
fi

# Create .venv with all dependency groups and enable commit hooks.
uv sync
uv run pre-commit install

# Browser for the e2e suite / dev harness. Installed from the synced venv so the
# browser build always matches the resolved playwright version; downloads land on
# the playwright-browsers volume, so rebuilds only re-run the (idempotent) apt deps.
uv run playwright install --with-deps chromium

# Icon collections for previews. Best-effort: don't fail container creation when
# the npm registry is unreachable.
bash scripts/fetch-icons.sh \
  || echo "WARN: icon fetch failed (offline?); rerun 'bash scripts/fetch-icons.sh' later"
