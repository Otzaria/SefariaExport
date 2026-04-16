#!/usr/bin/env bash
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN env is required}"

TAG="${TS_STAMP}"
shopt -s nullglob

file_size_bytes() {
  if stat -c%s "$1" >/dev/null 2>&1; then
    stat -c%s "$1"
  else
    stat -f%z "$1"
  fi
}

file_size_human() {
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$1"
  else
    printf '%s bytes' "$1"
  fi
}

FILES=( ./output/sefaria-exports-${TS_STAMP}.tar.zst.part-* )
if [ "${#FILES[@]}" -eq 0 ]; then
  FILES=( sefaria-exports-${TS_STAMP}.tar.zst.part-* )
fi

if [ "${#FILES[@]}" -eq 0 ] && [ -f "./output/sefaria-exports-${TS_STAMP}.tar.zst" ]; then
  FILES=( "./output/sefaria-exports-${TS_STAMP}.tar.zst" )
fi

if [ "${#FILES[@]}" -eq 0 ] && [ -f "sefaria-exports-${TS_STAMP}.tar.zst" ]; then
  FILES=( "sefaria-exports-${TS_STAMP}.tar.zst" )
fi

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "❌ No files to upload for tag ${TAG}"
  echo "Workspace root:"
  ls -lah .
  echo "Output directory:"
  ls -lah ./output 2>/dev/null || true
  exit 1
fi

echo "📤 Found ${#FILES[@]} file(s) to upload"

for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "❌ File disappeared before upload: $f"
    exit 1
  fi

  if [ ! -r "$f" ]; then
    echo "❌ File is not readable: $f"
    ls -l "$f" || true
    exit 1
  fi

  FILE_SIZE_BYTES="$(file_size_bytes "$f")"
  FILE_SIZE_HUMAN="$(file_size_human "$FILE_SIZE_BYTES")"
  echo "Uploading: ${f##*/} (${FILE_SIZE_HUMAN}) from $f"
  for attempt in {1..5}; do
    if gh release upload "$TAG" "$f" --clobber; then
      echo "✅ Uploaded: $f"
      break
    fi
    sleep $((2**attempt))
    echo "🔄 Retry $attempt for $f..."
    if [[ $attempt -eq 5 ]]; then
      echo "❌ Failed to upload $f after 5 attempts"
      exit 1
    fi
  done
done

echo "✅ All files uploaded successfully"
