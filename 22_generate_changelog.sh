#!/usr/bin/env bash
set -euo pipefail

# Diff this release's manifest vs the previous one; update notes, upload assets, post to forum.

: "${GH_TOKEN:?GH_TOKEN env is required}"
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
echo "📄 New manifest: $NEW_MANIFEST ($(wc -l < "$NEW_MANIFEST") files)"
echo "🔤 New titles:   ${NEW_TITLES:-<none>}"

# Newest non-draft release whose tag != this one.
PREV_TAG="$(
  gh release list --limit 100 --json tagName,isDraft,createdAt \
    --jq 'map(select(.isDraft|not)) | sort_by(.createdAt) | reverse | map(.tagName)[]' \
    2>/dev/null | grep -v -x -F "$TAG" | head -n1 || true
)"

OLD_MANIFEST="$WORKDIR/prev_manifest.txt"
OLD_TITLES="$WORKDIR/prev_titles.json"
rm -f "$OLD_MANIFEST" "$OLD_TITLES"
if [ -n "$PREV_TAG" ]; then
  echo "🔎 Previous release: $PREV_TAG — downloading its manifest + titles..."
  if gh release download "$PREV_TAG" -p manifest.txt -O "$OLD_MANIFEST" --clobber 2>/dev/null; then
    echo "✅ Got previous manifest ($(wc -l < "$OLD_MANIFEST") files)"
  else
    echo "⚠️  Previous release has no manifest.txt asset — treating as initial."
    : > "$OLD_MANIFEST"
  fi
  # Previous titles.json (optional) — Hebrew names for removed books.
  gh release download "$PREV_TAG" -p titles.json -O "$OLD_TITLES" --clobber 2>/dev/null \
    && echo "✅ Got previous titles.json" \
    || echo "ℹ️  Previous release has no titles.json (removed books fall back to English)."
else
  echo "ℹ️  No previous release found — treating as initial release."
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

# Changelog markdown + machine-readable diff for the forum step.
CHANGELOG="$WORKDIR/CHANGELOG.md"
DIFF_JSON="$WORKDIR/changelog_diff.json"
CL_ARGS=( --new-tag "$TAG" --json "$DIFF_JSON" )
[ -n "$PREV_TAG" ] && CL_ARGS+=( --old-tag "$PREV_TAG" )
[ -n "$NEW_TITLES" ] && CL_ARGS+=( --titles "$NEW_TITLES" )
[ -s "$OLD_TITLES" ] && CL_ARGS+=( --prev-titles "$OLD_TITLES" )
[ -f "$BLACKLIST" ] && CL_ARGS+=( --blacklist "$BLACKLIST" )
[ -f "$VERSIONS_BLACKLIST" ] && CL_ARGS+=( --versions-blacklist "$VERSIONS_BLACKLIST" )
[ -d "$EXPORTS_DIR" ] && CL_ARGS+=( --exports-dir "$EXPORTS_DIR" )
python3 "$WORKDIR/generate_changelog.py" \
  "$OLD_MANIFEST" "$NEW_MANIFEST" "$CHANGELOG" "${CL_ARGS[@]}"

echo "----- CHANGELOG.md -----"
sed -n '1,40p' "$CHANGELOG"
echo "------------------------"


# Upload manifest, titles and changelog as assets.
UPLOADS=( "$NEW_MANIFEST" "$CHANGELOG" )
[ -n "$NEW_TITLES" ] && UPLOADS+=( "$NEW_TITLES" )
gh release upload "$TAG" "${UPLOADS[@]}" --clobber
echo "✅ Uploaded ${UPLOADS[*]} to $TAG"

# Publish to the Otzaria forum (gated/non-fatal, see post_to_forum.py).
FORUM_ARGS=( "$DIFF_JSON" --tag "$TAG" --topic "${FORUM_TOPIC_ID:-1617}" )
[ -n "$NEW_TITLES" ] && FORUM_ARGS+=( --titles "$NEW_TITLES" )
[ -s "$OLD_TITLES" ] && FORUM_ARGS+=( --prev-titles "$OLD_TITLES" )
python3 "$WORKDIR/post_to_forum.py" "${FORUM_ARGS[@]}"
