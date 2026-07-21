#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Granola: Personal
# @raycast.mode compact
# @raycast.packageName Granola Switcher
# @raycast.icon ../granola_switcher/data/panda-switcher.png

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

"$REPO_DIR/bin/granola-switcher" switch personal
