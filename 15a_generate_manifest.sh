#!/usr/bin/env bash
set -euo pipefail

# Write output/manifest.txt: one `sha256  <path>` line per export file (the diff baseline).

EXPORTS_DIR="${SEFARIA_EXPORT_PATH:-${GITHUB_WORKSPACE:-$PWD}/exports}"
OUT_DIR="${GITHUB_WORKSPACE:-$PWD}/output"
mkdir -p "$OUT_DIR"
MANIFEST="$OUT_DIR/manifest.txt"

if [ ! -d "$EXPORTS_DIR" ]; then
  echo "❌ exports directory not found: $EXPORTS_DIR"
  exit 1
fi

echo "🔢 Hashing files under $EXPORTS_DIR ..."
# Sort first (NUL-safe) for a deterministic manifest.
( cd "$EXPORTS_DIR" && find . -type f -print0 | sort -z | xargs -0 sha256sum ) > "$MANIFEST"

LINES=$(wc -l < "$MANIFEST")
SIZE=$(du -h "$MANIFEST" | cut -f1)
echo "✅ Manifest written: $MANIFEST (${LINES} files, ${SIZE})"

# titles.json (English -> Hebrew title) for the forum post; non-fatal.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "🔤 Extracting Hebrew titles..."
python3 "$SCRIPT_DIR/extract_titles.py" || echo "⚠️  title extraction failed (non-fatal)"
