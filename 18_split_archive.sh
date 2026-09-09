#!/usr/bin/env bash
set -euo pipefail

COMBINED="sefaria-exports-${TS_STAMP}.tar.zst"

if [ ! -f "${COMBINED}" ]; then
  echo "❌ Archive not found: ${COMBINED}"
  exit 1
fi

# Size helpers — see 17_build_combined_archive.sh for the full reasoning.  In
# short: `stat -c%s` and `numfmt` are GNU-only, this step is a documented local
# macOS command in README as well as a CI step, and each helper probes the GNU
# form once so the Linux output is unchanged.  They are duplicated rather than
# sourced because the repo has no shared shell library and every numbered script
# is run standalone; only the two helpers this script calls are here, since an
# uncalled `dir_size_bytes` would be dead code in a pipeline step.
if stat -c%s . >/dev/null 2>&1; then
  STAT_SIZE_FLAG='-c%s'   # GNU coreutils
elif stat -f%z . >/dev/null 2>&1; then
  STAT_SIZE_FLAG='-f%z'   # BSD / macOS
else
  echo "❌ stat cannot report a file size: neither GNU 'stat -c%s' nor BSD 'stat -f%z' works" >&2
  exit 1
fi

file_size_bytes() {
  stat "${STAT_SIZE_FLAG}" "$1"
}

if numfmt --to=iec-i --suffix=B 1 >/dev/null 2>&1; then
  human_size() {
    numfmt --to=iec-i --suffix=B "$1"
  }
else
  human_size() {
    awk -v bytes="$1" '
      function ceil(x) { return (x == int(x)) ? x : int(x) + 1 }
      function iec(v) { return (ceil(v * 10) / 10 < 10) ? ceil(v * 10) / 10 : ceil(v) }
      BEGIN {
        split("B KiB MiB GiB TiB PiB EiB", unit, " ")
        i = 1
        while (bytes >= 1024 && i < 7) { bytes /= 1024; i++ }
        if (i == 1) { printf "%d%s\n", bytes, unit[i]; exit }
        rounded = iec(bytes)
        if (rounded >= 1024 && i < 7) { bytes /= 1024; i++; rounded = iec(bytes) }
        if (rounded < 10) { printf "%.1f%s\n", rounded, unit[i] }
        else { printf "%d%s\n", rounded, unit[i] }
      }'
  }
fi

FILE_SIZE=$(file_size_bytes "${COMBINED}")
echo "📊 Archive size: ${FILE_SIZE} bytes ($(human_size "${FILE_SIZE}"))"

# Si < 1.9GB, pas besoin de split
if [ "${FILE_SIZE}" -lt 1900000000 ]; then
  echo "✅ File is small enough, no splitting needed"
else
  echo "✂️  Splitting into parts..."
  split -b 1900m -d -a 2 "${COMBINED}" "${COMBINED}.part-"
  rm "${COMBINED}"  # Supprimer l'original après split
  ls -lh "${COMBINED}".part-*
fi
