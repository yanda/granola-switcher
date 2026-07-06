#!/bin/bash
# @raycast.schemaVersion 1
# @raycast.title Granola: Selected Account
# @raycast.mode compact
# @raycast.packageName Granola Switcher
# @raycast.icon ../granola_switcher/data/granola.png

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

"$REPO_DIR/bin/granola-switcher" selected
