#!/usr/bin/env bash
set -euo pipefail

# Build two changelogs before the draft release is created:
#   changelog_diff.json       complete, machine-readable, never filtered
#   forum_changelog_diff.json display-only and allowed to use blacklists

: "${TS_STAMP:?TS_STAMP env is required}"

TAG="${TS_STAMP}"
WORKDIR="${GITHUB_WORKSPACE:-$PWD}"

# Locate an artifact in repo root or ./output.
find_artifact() {
  for cand in "$WORKDIR/$1" "$WORKDIR/output/$1"; do
    if [ -f "$cand" ]; then echo "$cand"; return 0; fi
  done
  return 1
}
NEW_MANIFEST="$(find_artifact manifest.txt || true)"
if [ -z "$NEW_MANIFEST" ]; then
  echo "❌ New manifest.txt not found (looked in repo root and ./output)"
  exit 1
fi
NEW_TITLES="$(find_artifact titles.json || true)"
if [ -z "$NEW_TITLES" ] || [ ! -s "$NEW_TITLES" ]; then
  echo "❌ New titles.json is required and must be non-empty"
  exit 1
fi
echo "📄 New manifest: $NEW_MANIFEST ($(wc -l < "$NEW_MANIFEST") files)"
echo "🔤 New titles:   ${NEW_TITLES:-<none>}"

PREV_TAG="${PREVIOUS_TAG:-}"

OLD_MANIFEST="$WORKDIR/prev_manifest.txt"
OLD_TITLES="$WORKDIR/prev_titles.json"
rm -f "$OLD_MANIFEST" "$OLD_TITLES"
if [ -n "$PREV_TAG" ]; then
  echo "🔎 Previous release: $PREV_TAG — downloading its manifest + titles..."
  # Download under the original asset names: file_descriptor() includes the name,
  # so validating a renamed copy would always fail the contract check.
  PREV_ASSETS="$WORKDIR/previous-assets"
  rm -rf "$PREV_ASSETS"
  mkdir -p "$PREV_ASSETS"
  gh release download "$PREV_TAG" -p manifest.txt -O "$PREV_ASSETS/manifest.txt"
  gh release download "$PREV_TAG" -p titles.json -O "$PREV_ASSETS/titles.json"
  PYTHONPATH="$WORKDIR" python3 - "$WORKDIR/previous-release/release_metadata.json" "$PREV_ASSETS/manifest.txt" "$PREV_ASSETS/titles.json" <<'PY'
import json
import sys
from pathlib import Path

from release_contract import ContractError, file_descriptor

metadata = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for field, path in (("manifest", Path(sys.argv[2])), ("titles", Path(sys.argv[3]))):
    if file_descriptor(path) != metadata[field]:
        raise ContractError(f"downloaded previous {field} differs from release metadata")
PY
  mv "$PREV_ASSETS/manifest.txt" "$OLD_MANIFEST"
  mv "$PREV_ASSETS/titles.json" "$OLD_TITLES"
  echo "✅ Got previous manifest ($(wc -l < "$OLD_MANIFEST") files) and titles.json"
else
  echo "ℹ️  Explicit initial baseline — no previous release."
  : > "$OLD_MANIFEST"
fi

# Fetch an optional blacklist from the SeforimLibrary repo; a fetch failure is non-fatal
# ($2 removed, $4 printed). Both blacklists live there — the fork's main branch is `otzaria`.
fetch_optional_list() {  # $1=url $2=dest $3=success-label $4=absence-note
  rm -f "$2"
  if curl -fsSL --retry 3 --max-time 60 "$1" -o "$2"; then
    echo "🚫 $3 fetched: $(grep -cvE '^[[:space:]]*(#|$)' "$2") entries from $1"
  else
    echo "$4"
    rm -f "$2"
  fi
}

# Books on the books blacklist are never imported, so they are dropped from the changelog/forum.
BLACKLIST="$WORKDIR/books_blacklist.txt"
BLACKLIST_URL="${BOOKS_BLACKLIST_URL:-https://raw.githubusercontent.com/Otzaria/SeforimLibrary/otzaria/generator/sefariasqlite/src/jvmMain/resources/books_blacklist.txt}"
fetch_optional_list "$BLACKLIST_URL" "$BLACKLIST" "Blacklist" \
  "⚠️  Could not fetch blacklist from $BLACKLIST_URL — publishing WITHOUT blacklist filtering."

# The versions blacklist keeps already-excluded editions out of the "new book versions" report.
VERSIONS_BLACKLIST="$WORKDIR/black_versions.txt"
VERSIONS_BLACKLIST_URL="${VERSIONS_BLACKLIST_URL:-https://raw.githubusercontent.com/Otzaria/SeforimLibrary/otzaria/generator/sefariasqlite/src/jvmMain/resources/black_versions.txt}"
fetch_optional_list "$VERSIONS_BLACKLIST_URL" "$VERSIONS_BLACKLIST" "Versions blacklist" \
  "ℹ️  No versions blacklist at $VERSIONS_BLACKLIST_URL — reporting all new versions."

# Exports dir (bind-mounted, persists after the container exits) — read to resolve each new
# version's exact versionTitle, since the on-disk filename is sanitized and would not match.
EXPORTS_DIR="${SEFARIA_EXPORT_PATH:-$WORKDIR/exports}"

# The machine diff is deliberately unfiltered.  Rename continuity must not
# depend on a network blacklist or on forum presentation policy.
CHANGELOG="$WORKDIR/CHANGELOG.md"
DIFF_JSON="$WORKDIR/changelog_diff.json"
MACHINE_MD="$WORKDIR/CHANGELOG.machine.md"
MACHINE_ARGS=( --new-tag "$TAG" --json "$DIFF_JSON" )
[ -n "$PREV_TAG" ] && MACHINE_ARGS+=( --old-tag "$PREV_TAG" )
[ -n "$NEW_TITLES" ] && MACHINE_ARGS+=( --titles "$NEW_TITLES" )
[ -s "$OLD_TITLES" ] && MACHINE_ARGS+=( --prev-titles "$OLD_TITLES" )
[ -d "$EXPORTS_DIR" ] && MACHINE_ARGS+=( --exports-dir "$EXPORTS_DIR" )
python3 "$WORKDIR/generate_changelog.py" \
  "$OLD_MANIFEST" "$NEW_MANIFEST" "$MACHINE_MD" "${MACHINE_ARGS[@]}"

# A separately generated display copy may be filtered.  It is never consumed
# by sync-manual-links and is not referenced from release_metadata.json.
FORUM_JSON="$WORKDIR/forum_changelog_diff.json"
FORUM_ARGS=( --new-tag "$TAG" --json "$FORUM_JSON" )
[ -n "$PREV_TAG" ] && FORUM_ARGS+=( --old-tag "$PREV_TAG" )
[ -n "$NEW_TITLES" ] && FORUM_ARGS+=( --titles "$NEW_TITLES" )
[ -s "$OLD_TITLES" ] && FORUM_ARGS+=( --prev-titles "$OLD_TITLES" )
[ -f "$BLACKLIST" ] && FORUM_ARGS+=( --blacklist "$BLACKLIST" )
[ -f "$VERSIONS_BLACKLIST" ] && FORUM_ARGS+=( --versions-blacklist "$VERSIONS_BLACKLIST" )
[ -d "$EXPORTS_DIR" ] && FORUM_ARGS+=( --exports-dir "$EXPORTS_DIR" )
python3 "$WORKDIR/generate_changelog.py" \
  "$OLD_MANIFEST" "$NEW_MANIFEST" "$CHANGELOG" "${FORUM_ARGS[@]}"

echo "----- CHANGELOG.md -----"
sed -n '1,40p' "$CHANGELOG"
echo "------------------------"


test -s "$DIFF_JSON"
test -s "$FORUM_JSON"
test -s "$CHANGELOG"
