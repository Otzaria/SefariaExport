#!/usr/bin/env bash
set -euo pipefail

# Bundle referenced images for the library build. Image failures are recorded
# in exports/metadata/textimages_report.json and kept out of the changelog.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${GITHUB_WORKSPACE:-$PWD}"
python3 "$SCRIPT_DIR/16a_fetch_textimages.py"
