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

# Changelog markdown + machine-readable diff for the forum step.
CHANGELOG="$WORKDIR/CHANGELOG.md"
DIFF_JSON="$WORKDIR/changelog_diff.json"
CL_ARGS=( --new-tag "$TAG" --json "$DIFF_JSON" )
[ -n "$PREV_TAG" ] && CL_ARGS+=( --old-tag "$PREV_TAG" )
python3 "$WORKDIR/generate_changelog.py" \
  "$OLD_MANIFEST" "$NEW_MANIFEST" "$CHANGELOG" "${CL_ARGS[@]}"

echo "----- CHANGELOG.md -----"
sed -n '1,40p' "$CHANGELOG"
echo "------------------------"

# Prepend the changelog to the existing notes.
EXISTING_NOTES="$(gh release view "$TAG" --json body --jq '.body' 2>/dev/null || true)"
NOTES_FILE="$WORKDIR/RELEASE_NOTES.md"
{
  cat "$CHANGELOG"
  if [ -n "$EXISTING_NOTES" ]; then
    printf '\n\n---\n\n%s\n' "$EXISTING_NOTES"
  fi
} > "$NOTES_FILE"

gh release edit "$TAG" --notes-file "$NOTES_FILE"
echo "✅ Release notes updated for $TAG"

# Upload manifest, titles and changelog as assets.
UPLOADS=( "$NEW_MANIFEST" "$CHANGELOG" )
[ -n "$NEW_TITLES" ] && UPLOADS+=( "$NEW_TITLES" )
gh release upload "$TAG" "${UPLOADS[@]}" --clobber
echo "✅ Uploaded ${UPLOADS[*]} to $TAG"

# Publish to the Otzaria forum (gated/non-fatal, see post_to_forum.py).
FORUM_ARGS=( "$DIFF_JSON" --tag "$TAG" --topic "${FORUM_TOPIC_ID:-20}" )
[ -n "$NEW_TITLES" ] && FORUM_ARGS+=( --titles "$NEW_TITLES" )
[ -s "$OLD_TITLES" ] && FORUM_ARGS+=( --prev-titles "$OLD_TITLES" )
python3 "$WORKDIR/post_to_forum.py" "${FORUM_ARGS[@]}"
