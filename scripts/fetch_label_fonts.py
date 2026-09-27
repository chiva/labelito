#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch the selectable label fonts listed in app/render/font_manifest.json into DEST.

Every download is pinned (google/fonts at a fixed commit, DSEG at a release tag) and verified against
the manifest's SHA-256 before it is used; an archive member is verified again after extraction. A
mismatch aborts before anything is written. Each family lands in ``DEST/<key>/`` with its font files
and ``LICENSE.txt`` (OFL-1.1 and Apache-2.0 both require the licence to travel with the font). The
tree is staged in a temporary directory and swapped into DEST only after every file verified, so a
failed or partial run never leaves a half-populated DEST, and a rerun never leaves stale files.

The Docker ``label-fonts`` stage runs this at image build time: the running app never downloads
anything. Standard library only, so that stage needs no project dependencies.

Usage:
  scripts/fetch_label_fonts.py [DEST]          fetch into DEST (default: ./label-fonts, git-ignored)
  scripts/fetch_label_fonts.py --verify [DEST] check an existing DEST against the manifest, no network
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "app" / "render" / "font_manifest.json"
DEFAULT_DEST = ROOT / "label-fonts"
LICENSE_FILE_NAME = "LICENSE.txt"
DOWNLOAD_TIMEOUT_S = 60
EXIT_OK = 0
EXIT_MISMATCH = 1


class FontFetchError(RuntimeError):
    """A download failed or did not match its pinned SHA-256."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _planned_files(manifest: dict) -> list[tuple[str, dict]]:
    """(relative destination path, source entry) for every file the manifest ships."""
    planned: list[tuple[str, dict]] = []
    for family in manifest["families"]:
        if family.get("builtin"):
            continue
        key = family["key"]
        seen: set[str] = set()
        for style in family["styles"].values():
            if style["file"] not in seen:
                seen.add(style["file"])
                planned.append((f"{key}/{style['file']}", style))
        planned.append((f"{key}/{LICENSE_FILE_NAME}", family["license_file"]))
    return planned


def _expected_sha(entry: dict) -> str:
    return entry.get("member_sha256", entry["sha256"])


def _download(url: str, expected_sha: str, cache: dict[str, bytes]) -> bytes:
    if url not in cache:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:
            data = response.read()
        if _sha256(data) != expected_sha:
            raise FontFetchError(
                f"{url}: SHA-256 {_sha256(data)} does not match pinned {expected_sha}"
            )
        cache[url] = data
    return cache[url]


def _resolve(entry: dict, cache: dict[str, bytes]) -> bytes:
    data = _download(entry["url"], entry["sha256"], cache)
    member = entry.get("member")
    if member is None:
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        extracted = archive.read(member)
    if _sha256(extracted) != entry["member_sha256"]:
        raise FontFetchError(f"{entry['url']}!{member}: extracted file does not match its pin")
    return extracted


def fetch(dest: Path, manifest: dict) -> int:
    planned = _planned_files(manifest)
    cache: dict[str, bytes] = {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as work:
        stage = Path(work) / "out"
        for relative, entry in planned:
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_resolve(entry, cache))
            print(f"  ✓ {relative}")
        previous = Path(work) / "previous"
        if dest.exists():
            dest.rename(previous)
        stage.rename(dest)
    total = sum((dest / relative).stat().st_size for relative, _ in planned)
    print(f"→ {len(planned)} files, {total / 1_000_000:.1f} MB in {dest}")
    return EXIT_OK


def verify(dest: Path, manifest: dict) -> int:
    problems = []
    for relative, entry in _planned_files(manifest):
        path = dest / relative
        if not path.is_file():
            problems.append(f"missing {relative}")
        elif _sha256(path.read_bytes()) != _expected_sha(entry):
            problems.append(f"SHA-256 mismatch {relative}")
    for problem in problems:
        print(f"  ✗ {problem}", file=sys.stderr)
    if problems:
        return EXIT_MISMATCH
    print(f"→ {dest} matches the manifest")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", nargs="?", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--verify", action="store_true", help="check DEST offline, do not fetch")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.verify:
        return verify(args.dest, manifest)
    try:
        return fetch(args.dest, manifest)
    except (FontFetchError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
