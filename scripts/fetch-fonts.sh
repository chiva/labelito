#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Fetch DejaVu Sans (the font the printed label uses) into FONTS_DIR for local, non-Docker dev.
# The Docker image installs the same font via the `fonts-dejavu-core` apt package, so this script is
# only needed on a dev host (macOS/Windows, or a Linux box without DejaVu) where a preview would
# otherwise fall back to a different OS font — or PIL's tofu-only bitmap — and not match the print.
#
# DejaVu ships under the DejaVu Fonts License (Bitstream Vera + Arev terms): permissive,
# redistributable, GPL-compatible. The LICENSE file is fetched alongside the TTFs.
#
# The version is pinned below (DejaVu releases rarely; bump by hand). Downloaded over HTTPS from the
# upstream GitHub release; each extracted TTF is then validated (correct DejaVu family + the U+2192
# arrow glyph that motivated this) so a corrupt or substituted download aborts before it is used.
#
# Usage:  scripts/fetch-fonts.sh [DEST]
#   DEST defaults to ./fonts (git-ignored). Requires: curl, tar (bsdtar/GNU). Glyph validation runs
#   if Pillow is importable (it is inside the project venv via `uv run`); it is skipped otherwise.
set -euo pipefail

VERSION="2.37"
TARBALL="dejavu-fonts-ttf-${VERSION}.tar.bz2"
URL="https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/${TARBALL}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/fonts}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "→ downloading DejaVu ${VERSION}"
curl -fsSL "$URL" -o "$WORK/$TARBALL"

echo "→ extracting"
tar -xjf "$WORK/$TARBALL" -C "$WORK"
src="$WORK/dejavu-fonts-ttf-${VERSION}"

mkdir -p "$DEST"
cp "$src/ttf/DejaVuSans.ttf" "$DEST/DejaVuSans.ttf"
cp "$src/ttf/DejaVuSans-Bold.ttf" "$DEST/DejaVuSans-Bold.ttf"
cp "$src/LICENSE" "$DEST/LICENSE"

# Validate with the project venv (which has Pillow) when uv is present, else a bare python3; the
# validator self-skips if Pillow is unavailable so a missing dependency never blocks the install.
echo "→ validating fonts"
runner=(python3)
if command -v uv >/dev/null 2>&1; then runner=(uv run python); fi
( cd "$ROOT" && "${runner[@]}" - "$DEST" <<'PY'
import sys
from pathlib import Path

try:
    from PIL import ImageFont
except ImportError:
    print("  (Pillow not importable — skipping glyph validation)")
    sys.exit(0)

dest = Path(sys.argv[1])
for fname, _bold in (("DejaVuSans.ttf", False), ("DejaVuSans-Bold.ttf", True)):
    font = ImageFont.truetype(str(dest / fname), 32)
    family, _style = font.getname()
    if "DejaVu Sans" not in family:
        sys.exit(f"unexpected font family in {fname}: {family!r}")
    # U+2192 RIGHTWARDS ARROW — the glyph whose absence (bitmap fallback) motivated this script.
    left, _top, right, _bottom = font.getbbox("→")
    if right - left <= 0:
        sys.exit(f"{fname} has no glyph for U+2192 (arrow)")
print("✓ fonts valid")
PY
)

echo "✓ DejaVu ${VERSION} written to $DEST"
