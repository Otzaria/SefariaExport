#!/usr/bin/env bash
set -euo pipefail

# Bundle the images the export references (exports/textimages/), so the library
# build never needs to reach textimages.sefaria.org. Runs after the manifest so
# the manifest/changelog diff stays about book content. Best effort.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${GITHUB_WORKSPACE:-$PWD}"
python3 "$SCRIPT_DIR/16a_fetch_textimages.py"
