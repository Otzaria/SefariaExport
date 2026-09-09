#!/usr/bin/env bash
set -euo pipefail

cd "${GITHUB_WORKSPACE:-$PWD}"
COMBINED="sefaria-exports-${TS_STAMP}.tar.zst"

# Size helpers.  In CI this script runs inside the ubuntu:24.04 image, where
# GNU coreutils are guaranteed — but README documents a local run on macOS too,
# and there `stat -c%s`, `du -sb` and `numfmt` do not exist.  Each helper probes
# the GNU form once, up front, and keeps it whenever it works, so on Linux the
# printed strings, the numbers behind them and the abort-on-failure behaviour
# under `set -euo pipefail` are byte-for-byte what they were.
#
# Probing beats simply letting the GNU form fail and catching it: a `du -sb`
# that dies half way through (an unreadable subdirectory) still prints a partial
# total, and must keep aborting the run rather than silently sliding into a
# second, differently-measured number.
#
# The repo has no shared shell library — no script sources another, and README
# invokes every numbered step standalone — so 18_split_archive.sh carries its
# own copy of the two helpers it uses instead of sourcing these.
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

# `du -sb` is the apparent size in bytes; GNU du contributes 0 for a directory,
# so summing the regular files gives the identical total for a tree of plain
# files — measured on a 4-file fixture, 2831211 both ways.  The two only part
# company on hardlinks (du counts the inode once, the sum counts every link) and
# symlinks (du counts the link's own size, `-type f` skips it); the exporter
# writes neither, and this number feeds a log line and the RATIO, never an
# artifact.  /dev/null is already a hard requirement of this script (the find
# below redirects to it), so probing against it adds no new dependency and —
# unlike probing against exports/ — costs no directory walk.
if du -sb /dev/null >/dev/null 2>&1; then
  dir_size_bytes() {
    du -sb "$1" | cut -f1
  }
else
  dir_size_bytes() {
    find "$1" -type f -exec stat "${STAT_SIZE_FLAG}" {} + |
      awk '{ total += $1 } END { printf "%d\n", total }'
  }
fi

# numfmt is GNU-only.  The awk branch reproduces `--to=iec-i --suffix=B` exactly
# rather than approximating it: scale by 1024, round away from zero, one decimal
# below 10 and none at or above it, and promote a unit when that final rounded
# value reaches 1024 — 1047553 B is "1.0MiB", not "1024KiB", while exactly
# 1047552 B is "1023KiB".  Verified against numfmt on 10,119 values — every unit
# boundary plus 10k random ones — with zero mismatches below 2^53, where awk's
# doubles stop being exact on integers; the archive is ~2 GiB.  The probe runs
# numfmt instead of asking `command -v`, so a numfmt that exists but cannot do
# `--to=iec-i` also lands on the fallback.
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

# Verify that the exports directory contains files
FILE_COUNT=$(find exports -type f 2>/dev/null | wc -l)
echo "📊 Found ${FILE_COUNT} files in exports/"

if [ "${FILE_COUNT}" -eq 0 ]; then
  echo "❌ No files found in exports directory!"
  exit 1
fi

# How many zstd workers to start.
#
# `-T0` does NOT mean "use every CPU": it resolves to the number of PHYSICAL
# cores (zstd's UTIL_countPhysicalCores(), which de-duplicates /proc/cpuinfo by
# "core id"), not the logical CPUs the scheduler will actually hand us.  On a
# 4-vCPU GitHub runner that is 2 workers — run 33987734987 logged
# "Note: 2 physical core(s) detected" and then spent 20m13s here, 41% of the
# whole job.  `nproc` reports all 4.  Copied from LinkerToOtzaria's
# ci/zstd_mt.sh so both halves of the pipeline share one policy.
#
# Byte-neutral: zstd's frame output depends on the compression level, the job
# size and the overlap size — NOT on how many workers chew through the jobs.
# Verified on a 202 MiB tar: -T1/-T2/-T4 give an identical sha256, with both
# the old and the new flag set.
zstd_workers() {
  local n
  n="$(nproc 2>/dev/null || echo 0)"
  case "$n" in
    ''|*[!0-9]*) n=0 ;;
  esac
  # Bound worst-case resident set: each worker holds roughly one job buffer plus
  # one match-finder context.  0 falls back to zstd's own detection (i.e. -T0).
  if [ "$n" -gt 32 ]; then
    n=32
  fi
  printf '%s\n' "$n"
}
WORKERS="$(zstd_workers)"
if [ "${WORKERS}" -eq 0 ]; then
  WORKERS_LABEL="auto (-T0)"
else
  WORKERS_LABEL="${WORKERS}"
fi

# Archive all the contents of the exports directory.
# -19 --long=27 is nearly the same ratio as --ultra -22 but 5-10× faster; the
# level and window stay put because two downstream jobs download this asset.
# -B16M --zstd=ovlog=6 shrinks the compression jobs — zstd's default here is
# 64 MiB jobs at full overlap (ovlog=9), so every worker re-chews a whole job's
# worth of context.  Measured at -19/-T4 on two synthetic export corpora
# (105 MiB and 202 MiB): 1.4-1.8× faster for +0.8-1.6% bytes.  -B4M is a further
# ~15% faster but costs +2.2-4.7%, i.e. roughly 3× the bytes for a couple of
# seconds — and this asset is downloaded by two jobs every cycle and then kept
# forever on an immutable release, so the seconds are noise where the megabytes
# are not.  Pinning the geometry also stops the bytes drifting between zstd
# releases instead of inheriting a default that moves under us.
# --no-progress replaces -v: zstd still prints its one-line summary, without the
# 4,009 progress lines that -v emitted (13% of the entire job log).
EXPORT_BYTES=$(dir_size_bytes exports)
echo "📦 Compressing ${FILE_COUNT} files ($(human_size "${EXPORT_BYTES}")) from exports/ at zstd -19, ${WORKERS_LABEL} workers..."
SECONDS=0
tar -cf - -C exports . | zstd -19 --long=27 -B16M --zstd=ovlog=6 -T"${WORKERS}" --no-progress -o "${COMBINED}"
ELAPSED=${SECONDS}

ARCHIVE_BYTES=$(file_size_bytes "${COMBINED}")
RATIO=$(awk -v a="${ARCHIVE_BYTES}" -v e="${EXPORT_BYTES}" 'BEGIN{printf "%.2f", (e > 0 ? a * 100 / e : 0)}')
echo "✅ Archive created: ${COMBINED} — $(human_size "${ARCHIVE_BYTES}") (${RATIO}% of exports/) in ${ELAPSED}s"
