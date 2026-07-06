#!/bin/bash
# Release granola-switcher: bump version, tag, GitHub release, update tap formula.
set -euo pipefail

VERSION="${1:?usage: scripts/release.sh X.Y.Z [tap-dir]}"
TAP_DIR="${2:-$HOME/repos/homebrew-tap}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FORMULA="$TAP_DIR/Formula/granola-switcher.rb"

cd "$REPO_DIR"
if [ -n "$(git status --porcelain)" ]; then
  echo "working tree not clean" >&2
  exit 1
fi

python3 - "$VERSION" <<'PY'
import re
import sys
from pathlib import Path

version = sys.argv[1]
path = Path("granola_switcher/__init__.py")
path.write_text(re.sub(r'__version__ = ".*"', f'__version__ = "{version}"', path.read_text()))
PY

make test
git commit -am "Release v$VERSION"
git tag "v$VERSION"
git push origin main "v$VERSION"
gh release create "v$VERSION" --generate-notes

URL="https://github.com/yanda/granola-switcher/archive/refs/tags/v$VERSION.tar.gz"
SHA="$(curl -sL "$URL" | shasum -a 256 | cut -d' ' -f1)"

cd "$TAP_DIR"
sed -i '' -e "s|url \".*\"|url \"$URL\"|" -e "s|sha256 \".*\"|sha256 \"$SHA\"|" "$FORMULA"
git commit -am "granola-switcher $VERSION"
git push origin main
echo "Released v$VERSION (formula sha256 $SHA)"
