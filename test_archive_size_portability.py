"""The two archive steps report sizes with tools that only GNU coreutils has.

`17_build_combined_archive.sh` measures `exports/` with `du -sb`, the archive
with `stat -c%s`, and prints both through `numfmt --to=iec-i`;
`18_split_archive.sh` uses the last two.  In CI both run inside the ubuntu:24.04
image, where all three are guaranteed — but README's "You can run the pipeline
on Linux or macOS" makes `bash 17_build_combined_archive.sh` and
`bash 18_split_archive.sh` a documented local command on a machine that has none
of them (BSD `du` has no `-b`, BSD `stat` spells the size `-f%z`, and macOS
ships no `numfmt` at all).

Each script now probes for the GNU form once and falls back.  These tests pin
both halves of that: on the GNU path the scripts still print exactly what
`du`/`stat`/`numfmt` themselves say, and the fallback — reached by putting shims
that reject the GNU spellings first on PATH — prints the same strings.  The
awk humanizer is checked against `numfmt`'s own rounding at the unit boundaries,
because "1023KiB" and "1.0MiB" are one byte apart.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT_17 = ROOT / "17_build_combined_archive.sh"
SCRIPT_18 = ROOT / "18_split_archive.sh"
STAMP = "20260908T000000Z"
ARCHIVE = "sefaria-exports-%s.tar.zst" % STAMP

# `numfmt --to=iec-i --suffix=B` for each value, captured from GNU coreutils
# 9.4.  The pairs either side of a boundary are the interesting ones: numfmt
# rounds away from zero and promotes the unit when the *displayed* value would
# reach 1024, so 1047552 stays "1023KiB" and 1047553 becomes "1.0MiB".
IEC_SIZES = (
    (0, "0B"),
    (1023, "1023B"),
    (1024, "1.0KiB"),
    (1025, "1.1KiB"),
    (10230, "10KiB"),
    (10240, "10KiB"),
    (102400, "100KiB"),
    (1047552, "1023KiB"),
    (1047553, "1.0MiB"),
    (1048575, "1.0MiB"),
    (1048576, "1.0MiB"),
    (1234567, "1.2MiB"),
)

# A BSD/macOS `stat`: it has no `-c`, and spells the size as `-f%z`.
BSD_STAT_SHIM = """#!/usr/bin/env bash
case "${1:-}" in
  -f%z) shift ;;
  *) echo "stat: illegal option -- ${1#-}" >&2; exit 1 ;;
esac
for path in "$@"; do
  if [ -d "$path" ]; then echo 4096; else wc -c < "$path" | tr -d ' '; fi
done
"""

# A BSD/macOS `du`: no `-b`.  The fallback must not reach for it at all, so this
# one always fails; only the capability probe is expected to run it.
BSD_DU_SHIM = """#!/usr/bin/env bash
echo "du: illegal option -- b" >&2
exit 1
"""

# macOS has no `numfmt` whatsoever.  A shim that always fails stands in for that
# absence because the scripts probe by *running* numfmt, not by `command -v`.
NO_NUMFMT_SHIM = """#!/usr/bin/env bash
echo "numfmt: command not found" >&2
exit 127
"""

# Neither spelling works — the case the scripts must refuse by name.
BROKEN_STAT_SHIM = """#!/usr/bin/env bash
echo "stat: unrecognized option" >&2
exit 1
"""


def _runs(command):
    try:
        return subprocess.run(command, capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _write_shim(path, body):
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _shim_dir(directory, names):
    bindir = Path(directory) / "shim-bin"
    bindir.mkdir(exist_ok=True)
    for name, body in names.items():
        _write_shim(bindir / name, body)
    return bindir


def _env(shim_bin=None):
    env = os.environ.copy()
    env["TS_STAMP"] = STAMP
    # `numfmt` prints the locale's decimal separator -- "1,2MiB" under de_DE --
    # while awk's printf keeps the period, so IEC_SIZES (a C-locale table) only
    # describes both halves of the comparison while the children agree on a
    # locale.  CI is already C/UTF-8; this pins the same for a developer who is
    # not, instead of failing 6 subtests for a reason the scripts do not have.
    env["LC_ALL"] = "C"
    if shim_bin is not None:
        env["PATH"] = str(shim_bin) + os.pathsep + env["PATH"]
    return env


def _bash(script, cwd, env):
    return subprocess.run(
        ["bash", str(script)], cwd=str(cwd), env=env, text=True,
        capture_output=True, check=False,
    )


def _numfmt(value):
    return subprocess.run(
        ["numfmt", "--to=iec-i", "--suffix=B", str(value)],
        capture_output=True, text=True, check=True, env=_env(),
    ).stdout.strip()


def _scripts_runnable():
    """Can `bash` here actually execute a script out of this worktree?

    Not always: on Windows `bash` can resolve to the WSL launcher, which cannot
    open a `C:\\...` argument, and a checkout made with `core.autocrlf=true`
    before .gitattributes landed gives bash `set -euo pipefail\\r`.  Both are
    properties of the checkout, not of the scripts — CI checks out LF on Linux —
    so the runs below skip rather than go red for an unrelated reason.  The probe
    is the documented no-archive path: exit 1 and one specific line.
    """
    if shutil.which("bash") is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as workspace:
            probe = _bash(SCRIPT_18, workspace, _env())
            return probe.returncode == 1 and "Archive not found" in probe.stdout
    except OSError:
        return False


def _shims_usable():
    """Can a shell script dropped on PATH shadow a real tool here?  It cannot on
    every host that runs this suite, so the fallback tests skip rather than fail
    where the answer is no."""
    if shutil.which("bash") is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as directory:
            bindir = _shim_dir(directory, {"stat": BSD_STAT_SHIM})
            probe = Path(directory) / "probe.bin"
            probe.write_bytes(b"x" * 7)
            result = subprocess.run(
                ["bash", "-c", 'PATH="$1:$PATH"; stat -f%z "$2"', "probe",
                 str(bindir), str(probe)],
                capture_output=True, text=True, check=False,
            )
            return result.returncode == 0 and result.stdout.strip() == "7"
    except OSError:
        return False


HAVE_TAR_ZSTD = shutil.which("tar") is not None and shutil.which("zstd") is not None
HAVE_GNU_DU = _runs(["du", "-sb", str(SCRIPT_18)])
HAVE_GNU_STAT = _runs(["stat", "-c%s", str(SCRIPT_18)])
HAVE_NUMFMT = _runs(["numfmt", "--to=iec-i", "--suffix=B", "1"])
CAN_RUN = _scripts_runnable()
HAVE_SHIMS = CAN_RUN and _shims_usable()

ARCHIVE_LINE = re.compile(
    r"^✅ Archive created: (?P<name>\S+) — (?P<human>\S+) "
    r"\((?P<ratio>\d+\.\d{2})% of exports/\) in \d+s$"
)


def _exports_tree(root):
    """Three plain files in two subdirectories, with every mtime pinned so tar —
    which records them — produces the same bytes on every run."""
    exports = Path(root) / "exports"
    (exports / "a").mkdir(parents=True)
    (exports / "b").mkdir(parents=True)
    (exports / "a" / "one.json").write_bytes(b'{"a":"' + b"x" * 5000 + b'"}')
    (exports / "a" / "two.json").write_bytes(b'{"b":"' + b"y" * 123456 + b'"}')
    (exports / "b" / "three.json").write_bytes(b'{"c":"' + b"z" * 999 + b'"}')
    fixed = 1_600_000_000
    for path in sorted(exports.rglob("*"), reverse=True) + [exports]:
        os.utime(path, (fixed, fixed))
    return exports


class GnuPathIsUnchangedTest(unittest.TestCase):
    """Everything the scripts print on Linux is still the byte-for-byte output of
    `du -sb`, `stat -c%s` and `numfmt` — the helpers only choose between forms."""

    @unittest.skipUnless(
        CAN_RUN and HAVE_TAR_ZSTD and HAVE_GNU_DU and HAVE_GNU_STAT and HAVE_NUMFMT,
        "needs a runnable checkout, tar, zstd and GNU du/stat/numfmt",
    )
    def test_build_step_prints_du_sb_and_numfmt_verbatim(self):
        with tempfile.TemporaryDirectory() as workspace:
            exports = _exports_tree(workspace)
            env = _env()
            env["GITHUB_WORKSPACE"] = workspace
            result = _bash(SCRIPT_17, workspace, env)
            self.assertEqual(0, result.returncode, result.stderr)

            expected_bytes = subprocess.run(
                ["du", "-sb", "exports"], cwd=workspace, capture_output=True,
                text=True, check=True,
            ).stdout.split("\t")[0]
            lines = result.stdout.splitlines()
            self.assertEqual("📊 Found 3 files in exports/", lines[0])
            self.assertIn(
                "📦 Compressing 3 files (%s) from exports/ at zstd -19, "
                % _numfmt(expected_bytes),
                lines[1],
            )

            archive = Path(workspace) / ARCHIVE
            self.assertTrue(archive.is_file(), result.stdout)
            match = ARCHIVE_LINE.match(lines[-1])
            self.assertIsNotNone(match, lines[-1])
            self.assertEqual(ARCHIVE, match.group("name"))
            self.assertEqual(_numfmt(archive.stat().st_size), match.group("human"))
            self.assertAlmostEqual(
                archive.stat().st_size * 100 / int(expected_bytes),
                float(match.group("ratio")),
                places=2,
            )
            # `exports/` holds only plain files, so `du -sb` (which contributes 0
            # for a directory) and the fallback's per-file sum are the same total.
            self.assertEqual(
                sum(p.stat().st_size for p in exports.rglob("*") if p.is_file()),
                int(expected_bytes),
            )

    @unittest.skipUnless(CAN_RUN and HAVE_GNU_STAT and HAVE_NUMFMT,
                         "needs a runnable checkout and GNU stat/numfmt")
    def test_split_step_prints_numfmt_verbatim_at_every_boundary(self):
        for size, expected in IEC_SIZES:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as workspace:
                (Path(workspace) / ARCHIVE).write_bytes(b"\0" * size)
                result = _bash(SCRIPT_18, workspace, _env())
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, _numfmt(size))
                self.assertIn(
                    "📊 Archive size: %d bytes (%s)" % (size, expected),
                    result.stdout,
                )


class BsdFallbackTest(unittest.TestCase):
    """With GNU `du -sb`, `stat -c%s` and `numfmt` shimmed away, the same runs
    still succeed and still print the same sizes."""

    @unittest.skipUnless(HAVE_TAR_ZSTD and HAVE_SHIMS and HAVE_GNU_DU
                         and HAVE_GNU_STAT and HAVE_NUMFMT,
                         "needs tar, zstd, usable PATH shims and GNU du/stat/numfmt")
    def test_build_step_output_survives_the_loss_of_every_gnu_tool(self):
        with tempfile.TemporaryDirectory() as workspace:
            _exports_tree(workspace)
            env = _env()
            env["GITHUB_WORKSPACE"] = workspace

            gnu = _bash(SCRIPT_17, workspace, env)
            self.assertEqual(0, gnu.returncode, gnu.stderr)
            (Path(workspace) / ARCHIVE).unlink()

            bindir = _shim_dir(workspace, {
                "stat": BSD_STAT_SHIM, "du": BSD_DU_SHIM, "numfmt": NO_NUMFMT_SHIM,
            })
            bsd_env = _env(bindir)
            bsd_env["GITHUB_WORKSPACE"] = workspace

            # Not a vacuous run: under this PATH none of the three GNU forms
            # works, so a script that still reached for one would abort under
            # `set -euo pipefail` instead of printing anything.
            reachable = subprocess.run(
                ["bash", "-c",
                 "du -sb . >/dev/null 2>&1 && echo du; "
                 "stat -c%s . >/dev/null 2>&1 && echo stat; "
                 "numfmt --to=iec-i --suffix=B 1 >/dev/null 2>&1 && echo numfmt; "
                 "echo end"],
                cwd=workspace, env=bsd_env, text=True, capture_output=True, check=False,
            )
            self.assertEqual("end", reachable.stdout.strip())

            bsd = _bash(SCRIPT_17, workspace, bsd_env)
            self.assertEqual(0, bsd.returncode, bsd.stderr)

            # The input tree, and therefore the archive, is byte-identical across
            # the two runs; only the wall clock may differ.
            elapsed = re.compile(r" in \d+s$", re.M)
            self.assertEqual(
                elapsed.sub(" in Ns", gnu.stdout),
                elapsed.sub(" in Ns", bsd.stdout),
            )
            self.assertRegex(bsd.stdout, r"\(\d+(\.\d)?(B|KiB|MiB|GiB)\) from exports/")
            self.assertRegex(bsd.stdout, r"— \d+(\.\d)?(B|KiB|MiB|GiB) \(")

    @unittest.skipUnless(HAVE_SHIMS, "needs usable PATH shims")
    def test_the_awk_humanizer_reproduces_numfmt_at_every_boundary(self):
        shims = {"stat": BSD_STAT_SHIM, "du": BSD_DU_SHIM, "numfmt": NO_NUMFMT_SHIM}
        for size, expected in IEC_SIZES:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as workspace:
                (Path(workspace) / ARCHIVE).write_bytes(b"\0" * size)
                bindir = _shim_dir(workspace, shims)
                env = _env(bindir)
                reachable = subprocess.run(
                    ["bash", "-c",
                     "stat -c%s . >/dev/null 2>&1 && echo stat; "
                     "numfmt --to=iec-i --suffix=B 1 >/dev/null 2>&1 && echo numfmt; "
                     "echo end"],
                    cwd=workspace, env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual("end", reachable.stdout.strip())
                result = _bash(SCRIPT_18, workspace, env)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(
                    "📊 Archive size: %d bytes (%s)" % (size, expected),
                    result.stdout,
                )

    @unittest.skipUnless(HAVE_SHIMS, "needs usable PATH shims")
    def test_a_stat_with_neither_spelling_aborts_and_names_both(self):
        """Never a silent fallback: if no `stat` can report a size the step stops
        and says which two spellings it tried."""
        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / ARCHIVE).write_bytes(b"\0" * 1024)
            bindir = _shim_dir(workspace, {"stat": BROKEN_STAT_SHIM})
            result = _bash(SCRIPT_18, workspace, _env(bindir))
            self.assertNotEqual(0, result.returncode)
            self.assertIn("stat -c%s", result.stderr)
            self.assertIn("stat -f%z", result.stderr)
            self.assertNotIn("📊 Archive size", result.stdout)


class ScriptContractTest(unittest.TestCase):
    """Static guarantees that survive a refactor: strict mode, the pipefail-safe
    `nproc` guard, and a probe in front of every GNU-only tool."""

    def _text(self, path):
        return path.read_text(encoding="utf-8")

    def test_both_scripts_keep_strict_mode(self):
        for script in (SCRIPT_17, SCRIPT_18):
            self.assertIn("\nset -euo pipefail\n", self._text(script), script.name)

    def test_the_pipefail_safe_nproc_guard_is_untouched(self):
        # `nproc` is absent on macOS; `|| echo 0` keeps `set -e` from killing the
        # run and hands zstd its own detection via -T0.
        self.assertIn('n="$(nproc 2>/dev/null || echo 0)"', self._text(SCRIPT_17))

    def test_every_gnu_only_tool_sits_behind_a_probe(self):
        build, split = self._text(SCRIPT_17), self._text(SCRIPT_18)
        for text, name in ((build, SCRIPT_17.name), (split, SCRIPT_18.name)):
            self.assertIn("if stat -c%s . >/dev/null 2>&1; then", text, name)
            self.assertIn("elif stat -f%z . >/dev/null 2>&1; then", text, name)
            self.assertIn(
                "if numfmt --to=iec-i --suffix=B 1 >/dev/null 2>&1; then", text, name
            )
        self.assertIn("if du -sb /dev/null >/dev/null 2>&1; then", build)

    def test_the_measurements_go_through_the_helpers(self):
        build, split = self._text(SCRIPT_17), self._text(SCRIPT_18)
        self.assertIn("EXPORT_BYTES=$(dir_size_bytes exports)", build)
        self.assertIn('ARCHIVE_BYTES=$(file_size_bytes "${COMBINED}")', build)
        self.assertIn('$(human_size "${EXPORT_BYTES}")', build)
        self.assertIn('$(human_size "${ARCHIVE_BYTES}")', build)
        self.assertIn('FILE_SIZE=$(file_size_bytes "${COMBINED}")', split)
        self.assertIn('$(human_size "${FILE_SIZE}")', split)

    def test_the_two_copies_of_the_humanizer_stay_identical(self):
        """18 carries its own copy because the repo has no shared shell library.
        Duplication is only safe while the copies agree."""
        pattern = re.compile(r"awk -v bytes=\"\$1\" '(.*?)'\n", re.S)
        build = pattern.search(self._text(SCRIPT_17))
        split = pattern.search(self._text(SCRIPT_18))
        self.assertIsNotNone(build, SCRIPT_17.name)
        self.assertIsNotNone(split, SCRIPT_18.name)
        self.assertEqual(build.group(1), split.group(1))


if __name__ == "__main__":
    unittest.main()
