#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Fetch the bundled icon collections (FontAwesome Free, Material Symbols, Octicons) and normalize
# them into ICON_COLLECTIONS_DIR for the `icon` element's `collection` attribute. The Docker `icons`
# build stage runs the same pnpm install + normalize, so the dev and image layouts match.
#
# Versions are declared in package.json (caret ranges) and pinned exactly by pnpm-lock.yaml; bump
# them through Renovate PRs rather than by hand. pnpm settings (hoisted linker, 2-day release-age
# cooldown) live in pnpm-workspace.yaml; lifecycle scripts are disabled in .npmrc.
#
# Package pages (npm):
#   FontAwesome Free  https://www.npmjs.com/package/@fortawesome/fontawesome-free
#   Material Symbols  https://www.npmjs.com/package/@material-symbols/svg-400
#   Octicons          https://www.npmjs.com/package/@primer/octicons
#
# Usage:  scripts/fetch-icons.sh [DEST]
#   DEST defaults to assets/icon-collections (git-ignored). Requires: pnpm (via corepack).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/assets/icon-collections}"

# Install the pinned collections into node_modules: --frozen-lockfile installs exactly what the
# lockfile records (a caret range never floats at build time), --ignore-scripts is belt-and-braces
# over the .npmrc default. Run from the repo root so pnpm finds the manifest, lockfile, and settings.
echo "→ installing pinned icon packages with pnpm"
( cd "$ROOT" && pnpm install --frozen-lockfile --ignore-scripts )
modules="$ROOT/node_modules"

# Normalize each collection into a staging tree first, then swap into DEST only after every copy has
# succeeded. This keeps reruns reproducible: a version bump that renames or drops glyphs cannot leave
# stale SVGs behind (the per-collection dir is replaced wholesale), and a failure partway aborts
# under `set -e` before the swap, leaving an existing DEST intact.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/out"

# ── FontAwesome: keep the three styles as subdirectories (the element selects via `style`) ──
fa_src="$modules/@fortawesome/fontawesome-free"
for style in solid regular brands; do
  mkdir -p "$STAGE/fontawesome/$style"
  cp "$fa_src/svgs/$style/"*.svg "$STAGE/fontawesome/$style/"
done
cp "$fa_src/LICENSE.txt" "$STAGE/fontawesome/LICENSE.txt"

# ── Material Symbols: flatten the `outlined` weight, dropping the *-fill variants ──
material_src="$modules/@material-symbols/svg-400/outlined"
mkdir -p "$STAGE/material"
for svg in "$material_src"/*.svg; do
  case "$svg" in
    *-fill.svg) continue ;;
  esac
  cp "$svg" "$STAGE/material/"
done
cp "$modules/@material-symbols/svg-400/LICENSE" "$STAGE/material/LICENSE"

# ── Octicons: normalize <name>-<size>.svg to <name>.svg, preferring the 24px artwork ──
octicons_src="$modules/@primer/octicons/build/svg"
mkdir -p "$STAGE/octicons"
for svg in "$octicons_src"/*-24.svg; do
  name="$(basename "$svg" -24.svg)"
  cp "$svg" "$STAGE/octicons/$name.svg"
done
for svg in "$octicons_src"/*-16.svg; do
  name="$(basename "$svg" -16.svg)"
  [ -f "$STAGE/octicons/$name.svg" ] || cp "$svg" "$STAGE/octicons/$name.svg"
done
cp "$modules/@primer/octicons/LICENSE" "$STAGE/octicons/LICENSE"

# ── Swap staged collections into DEST, replacing each dir wholesale so no stale glyph survives ──
mkdir -p "$DEST"
for collection in fontawesome material octicons; do
  rm -rf "$DEST/$collection"
  mv "$STAGE/$collection" "$DEST/$collection"
done

echo "✓ icon collections written to $DEST"
