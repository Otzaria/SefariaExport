#!/usr/bin/env python3
"""Diff two export manifests into a Markdown changelog (+ optional --json diff for the forum).

Book identity is the English title (the last path segment, == titles.json key). On that
identity we classify, with exact-match only (no fuzzy/similarity guessing):
  • added / removed       — English title present on only one side
  • he-renamed            — same English title, different Hebrew title in titles.json
  • en-renamed            — a removed and an added title sharing an identical content sha256
  • moved                 — same English title, different category path
  • content-changed       — same English title, sha256 differs (its own axis: a book may
                            also appear under renamed/moved, so no change is ever hidden)
"""
import argparse
import json
import os
import re
import sys


def normalize_title_key(value):
    """Normalize a title to a match key, mirroring SeforimLibrary's normalizeTitleKey
    (drop quotes/geresh/gershayim, lowercase, collapse whitespace, '_'->' ') so our keys
    line up with the library's books_blacklist matching."""
    if value is None or not value.strip():
        return None
    without_quotes = (
        value.replace('"', "")
        .replace("'", "")
        .replace("׳", "")  # Hebrew geresh
        .replace("״", "")  # Hebrew gershayim
    )
    collapsed = re.sub(r"\s+", " ", without_quotes.lower()).replace("_", " ")
    return collapsed.strip()


def blacklist_lines(path):
    """Yield cleaned entries from a blacklist file: BOM/whitespace stripped, blanks and
    #-comments skipped, backslash-escaped quotes unescaped. Shared by both blacklists."""
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.lstrip("﻿").strip()
            if not line or line.startswith("#"):
                continue
            yield line.replace('\\"', '"').replace("\\'", "'")


def load_blacklist_keys(path):
    """Read books_blacklist.txt into a set of normalized keys."""
    return {key for line in blacklist_lines(path) if (key := normalize_title_key(line))}


def load_title_map(path):
    """Load one {English title: Hebrew title} map; tolerates a missing/empty file."""
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    return {}


def is_blacklisted(en, keys, *he_values):
    """Blacklisted when the English title or any provided Hebrew title matches a key."""
    for candidate in (en, *he_values):
        key = normalize_title_key(candidate)
        if key and key in keys:
            return True
    return False


def load_manifest(path):
    """Return {relative_path: sha256}; reject malformed or ambiguous input."""
    out = {}
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return out
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if len(line) < 67 or line[64:66] != "  ":
                raise ValueError(f"malformed manifest line {number} in {path}")
            digest = line[:64]
            filepath = line[66:]  # skip the 64-char hash and the two spaces
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not filepath:
                raise ValueError(f"malformed manifest line {number} in {path}")
            if filepath in out:
                raise ValueError(f"duplicate manifest path {filepath!r} in {path}")
            out[filepath] = digest
    return out


def classify(path):
    """Return (bucket, label): bucket is book|version|schema|link|toc|authors|other."""
    p = path[2:] if path.startswith("./") else path
    if p.startswith("json/") and p.endswith("/merged.json"):
        return "book", p[len("json/"):-len("/merged.json")]
    if p.startswith("json/") and p.endswith(".json"):
        # Per-version text file: json/<cats>/<title>/<versionTitle>.json
        return "version", p[len("json/"):-len(".json")]
    if p.startswith("schemas/") and p.endswith(".json"):
        return "schema", p[len("schemas/"):-len(".json")]
    if p.startswith("links/"):
        return "link", p[len("links/"):]
    if p == "table_of_contents.json":
        return "toc", "table_of_contents.json"
    if p == "authors.json":
        return "authors", "authors.json"
    return "other", p


def version_records(manifest):
    """classify-label -> {path, book_en, filename} for every per-version text file."""
    recs = {}
    for path in manifest:
        bucket, label = classify(path)
        if bucket != "version":
            continue
        segs = label.split("/")
        if len(segs) < 2:  # need at least <book>/<versionFile>
            continue
        recs[label] = {"path": path, "book_en": segs[-2], "filename": segs[-1]}
    return recs


def read_version_titles(exports_dir, manifest_path):
    """Exact (versionTitle, heVersionTitle) from the export file itself — the on-disk
    filename is sanitized (parens/quotes stripped) and would not match the library's
    exact key. (None, None) if unreadable."""
    if not exports_dir:
        return None, None
    rel = manifest_path[2:] if manifest_path.startswith("./") else manifest_path
    full = os.path.join(exports_dir, rel)
    try:
        with open(full, encoding="utf-8") as fh:
            doc = json.load(fh)
        vt = (doc.get("versionTitle") or "").strip() or None
        he = (doc.get("versionTitleInHebrew") or "").strip() or None
        return vt, he
    except Exception as e:
        print(f"⚠️  Could not read exact version title from {full}: {e}", file=sys.stderr)
        return None, None


def load_versions_blacklist(path):
    """Parse black_versions.txt → (global_keys, {book_key: {version_keys}}), mirroring the
    generator's VersionsBlacklist: '<version>' is global, '<book> | <version>' is scoped."""
    global_keys, per_book = set(), {}
    for line in blacklist_lines(path):
        if "|" in line:
            book, version = line.split("|", 1)
            bk, vk = normalize_title_key(book), normalize_title_key(version)
            if bk and vk:
                per_book.setdefault(bk, set()).add(vk)
        else:
            vk = normalize_title_key(line)
            if vk:
                global_keys.add(vk)
    return global_keys, per_book


def is_version_blacklisted(book_en, book_he, ver_en, ver_he, global_keys, per_book):
    vkeys = {normalize_title_key(ver_en), normalize_title_key(ver_he)} - {None}
    if vkeys & global_keys:
        return True
    for bk in (normalize_title_key(book_en), normalize_title_key(book_he)):
        if bk and vkeys & per_book.get(bk, set()):
            return True
    return False


def diff_versions(old, new, new_titles, exports_dir, book_keys, vbl):
    """Added per-version files not already excluded. Each: {book_en, book_he, version, exact}.
    Skips versions of blacklisted books and already-blacklisted versions."""
    old_recs, new_recs = version_records(old), version_records(new)
    added = sorted(set(new_recs) - set(old_recs))
    global_keys, per_book = vbl
    out = []
    for label in added:
        rec = new_recs[label]
        book_en = rec["book_en"]
        book_he = new_titles.get(book_en)
        if is_blacklisted(book_en, book_keys, book_he):
            continue  # whole book never imported → its versions are irrelevant
        ver, he_ver = read_version_titles(exports_dir, rec["path"])
        exact = ver is not None
        if not exact:
            ver = rec["filename"]  # best-effort fallback (warned above)
        if is_version_blacklisted(book_en, book_he, ver, he_ver, global_keys, per_book):
            continue
        out.append({"book_en": book_en, "book_he": book_he, "version": ver, "exact": exact})
    return out


def book_records(manifest):
    """English title -> {label, category, sha}; ambiguity is a hard failure."""
    recs, dups = {}, []
    for path, sha in manifest.items():
        bucket, label = classify(path)
        if bucket != "book":
            continue
        segs = label.split("/")
        en, category = segs[-1], "/".join(segs[:-1])
        if en in recs:
            dups.append(en)
        recs[en] = {"label": label, "category": category, "sha": sha}
    if dups:
        raise ValueError(
            f"duplicate English book title(s) in a manifest: "
            f"{', '.join(sorted(set(dups))[:10])}"
        )
    return recs


def non_book_counts(old, new):
    """Counts for the 'Also' note: links, version files, TOC, author names."""
    new_keys, old_keys = set(new), set(old)
    changed = {k for k in (new_keys & old_keys) if old[k] != new[k]}
    touched = (new_keys - old_keys) | (old_keys - new_keys) | changed
    links = sum(1 for p in touched if classify(p)[0] == "link")
    versions = sum(1 for p in touched if classify(p)[0] == "version")
    toc = any(classify(p)[0] == "toc" for p in touched)
    authors = any(classify(p)[0] == "authors" for p in touched)
    return links, versions, toc, authors


def diff_books(old_recs, new_recs, old_titles, new_titles):
    """Return the structured book diff (see module docstring for the categories)."""
    old_en, new_en = set(old_recs), set(new_recs)
    added_en = new_en - old_en
    removed_en = old_en - new_en
    common = old_en & new_en

    # en-rename: pair a removed and an added title by an identical content sha256.
    old_sha = {}
    for en in removed_en:
        old_sha.setdefault(old_recs[en]["sha"], []).append(en)
    en_renamed, paired_add, paired_rm = [], set(), set()
    for en in sorted(added_en):
        bucket = old_sha.get(new_recs[en]["sha"])
        # Only an unambiguous 1:1 sha match is a rename; never guess on collisions.
        if bucket and len(bucket) == 1 and bucket[0] not in paired_rm:
            old_en_name = bucket[0]
            en_renamed.append({
                "old_en": old_en_name, "new_en": en,
                "old_he": old_titles.get(old_en_name), "new_he": new_titles.get(en),
            })
            paired_add.add(en)
            paired_rm.add(old_en_name)

    he_renamed, moved, content = [], [], []
    for en in sorted(common):
        o, n = old_recs[en], new_recs[en]
        he_o, he_n = old_titles.get(en), new_titles.get(en)
        renamed = he_o is not None and he_n is not None and he_o != he_n
        movedp = o["category"] != n["category"]
        if renamed:
            he_renamed.append({"en": en, "old_he": he_o, "new_he": he_n})
        if movedp:
            moved.append({"en": en, "he": he_n or en,
                          "old_category": o["category"], "new_category": n["category"]})
        # Content is its own axis: any sha change is reported even alongside a rename
        # or a move, so a book that changed in several ways is never silently hidden.
        if o["sha"] != n["sha"]:
            content.append({"en": en, "he": he_n or en})

    return {
        "added": [{"en": en, "he": new_titles.get(en)} for en in sorted(added_en - paired_add)],
        "removed": [{"en": en, "he": old_titles.get(en)} for en in sorted(removed_en - paired_rm)],
        "he_renamed": he_renamed,
        "en_renamed": en_renamed,
        "moved": moved,
        "content_changed": content,
    }


def apply_blacklist(diff, keys):
    """Drop blacklisted books from every category; return the count removed."""
    if not keys:
        return 0
    dropped = 0

    def keep(en, *he):
        nonlocal dropped
        if is_blacklisted(en, keys, *he):
            dropped += 1
            return False
        return True

    diff["added"] = [b for b in diff["added"] if keep(b["en"], b["he"])]
    diff["removed"] = [b for b in diff["removed"] if keep(b["en"], b["he"])]
    diff["content_changed"] = [b for b in diff["content_changed"] if keep(b["en"], b["he"])]
    diff["moved"] = [b for b in diff["moved"] if keep(b["en"], b["he"])]
    diff["he_renamed"] = [b for b in diff["he_renamed"] if keep(b["en"], b["old_he"], b["new_he"])]
    diff["en_renamed"] = [b for b in diff["en_renamed"]
                          if keep(b["new_en"], b["new_he"]) and keep(b["old_en"], b["old_he"])]
    return dropped


def md_list(items, render, limit=400):
    """Render items via `render` as a Markdown bullet list, truncating very long lists."""
    lines = [f"- {render(it)}" for it in items[:limit]]
    if len(items) > limit:
        lines.append(f"- …and {len(items) - limit} more")
    return "\n".join(lines)


def _he(it, key="he"):
    return it.get(key) or it["en"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old_manifest")
    ap.add_argument("new_manifest")
    ap.add_argument("out_md")
    ap.add_argument("--new-tag", required=True)
    ap.add_argument("--old-tag", default="")
    ap.add_argument("--json", dest="json_out", default="",
                    help="also write a machine-readable diff for the forum step")
    ap.add_argument("--blacklist", default="",
                    help="books_blacklist.txt; matching books are dropped from the output")
    ap.add_argument("--titles", default="",
                    help="current release titles.json (English->Hebrew)")
    ap.add_argument("--prev-titles", dest="prev_titles", default="",
                    help="previous release titles.json (English->Hebrew)")
    ap.add_argument("--exports-dir", dest="exports_dir", default="",
                    help="exports dir; read to resolve exact versionTitle for new versions")
    ap.add_argument("--versions-blacklist", dest="versions_blacklist", default="",
                    help="black_versions.txt; already-listed versions are dropped from the output")
    ap.add_argument("--short-md", dest="short_md", default="",
                    help="also write a summary-only Markdown file (counts, no per-book lists) "
                         "for use as the GitHub release body")
    args = ap.parse_args()

    old = load_manifest(args.old_manifest)
    new = load_manifest(args.new_manifest)
    if not new:
        print("❌ New manifest is empty — nothing to diff", file=sys.stderr)
        return 1

    new_titles = load_title_map(args.titles)
    old_titles = load_title_map(args.prev_titles)

    book_keys = load_blacklist_keys(args.blacklist)
    diff = diff_books(book_records(old), book_records(new), old_titles, new_titles)
    dropped = apply_blacklist(diff, book_keys)
    if dropped:
        print(f"🚫 Blacklist: dropped {dropped} book entr{'y' if dropped == 1 else 'ies'} "
              f"from changelog & forum diff.")

    # New book editions (book_version) — reported so they can be triaged into
    # black_versions.txt. Exact versionTitle is read from the export files.
    # Skipped on an initial release (no baseline → every version looks "new").
    versions_blacklist = load_versions_blacklist(args.versions_blacklist)
    new_versions = diff_versions(
        old, new, new_titles, args.exports_dir, book_keys, versions_blacklist,
    ) if old else []
    if new_versions:
        inexact = sum(1 for v in new_versions if not v["exact"])
        print(f"🆕 New book versions: {len(new_versions)}"
              + (f" ({inexact} with filename-derived title — verify)" if inexact else ""))

    if args.json_out:
        payload = {
            "new_tag": args.new_tag,
            "old_tag": args.old_tag,
            "has_baseline": bool(old),
            "books": diff,
            "versions": {"added": new_versions},
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"✅ Diff JSON written: {args.json_out}")

    lines = [f"## What changed in `{args.new_tag}`", ""]
    if not old:
        lines += [
            "_Initial release — no previous manifest to compare against. "
            "Future releases will list added / removed / renamed / moved / changed books here._",
            "",
            f"- Total files in this release: **{len(new)}**",
        ]
        _write(args.out_md, "\n".join(lines))
        if args.short_md:
            _write(args.short_md, "\n".join(lines))
        print(f"✅ Changelog written (initial): {args.out_md}")
        return 0

    n = {k: len(v) for k, v in diff.items()}
    base = f"`{args.old_tag}`" if args.old_tag else "the previous release"
    lines += [
        f"Compared against {base}.", "",
        "| Change | Books |", "|---|---:|",
        f"| ➕ Added | {n['added']} |",
        f"| ➖ Removed | {n['removed']} |",
        f"| ✏️ Renamed | {n['he_renamed'] + n['en_renamed']} |",
        f"| 📂 Moved | {n['moved']} |",
        f"| 📝 Content changed | {n['content_changed']} |",
        "",
    ]
    links, versions, toc, authors = non_book_counts(old, new)
    note = []
    if links:
        note.append(f"{links} link table(s) regenerated")
    if versions:
        note.append(f"{versions} version file(s) added/updated/removed")
    if toc:
        note.append("table of contents updated")
    if authors:
        note.append("author names updated")
    if note:
        lines += ["_Also: " + ", ".join(note) + "._", ""]

    # The release body stops here: the per-book lists below can run to hundreds
    # of entries, and they are published to the forum (and to the CHANGELOG.md
    # asset) instead of being pasted into the release notes.
    if args.short_md:
        _write(args.short_md, "\n".join(lines + [
            "_Per-book detail is published to the Otzaria forum; the full list also ships "
            "as the `CHANGELOG.md` asset of this release._",
        ]))
        print(f"✅ Short release notes written: {args.short_md}")

    def section(title, items, render):
        if items:
            lines.extend([f"### {title} ({len(items)})", "", md_list(items, render), ""])

    section("➕ Added", diff["added"], lambda b: f"{_he(b)}  (`{b['en']}`)")
    section("➖ Removed", diff["removed"], lambda b: f"{_he(b)}  (`{b['en']}`)")
    section("✏️ Renamed (Hebrew title)", diff["he_renamed"],
            lambda b: f"`{b['en']}`: {b['old_he']} → {b['new_he']}")
    section("✏️ Renamed (English title)", diff["en_renamed"],
            lambda b: f"`{b['old_en']}` → `{b['new_en']}`  ({_he(b, 'new_he')})")
    section("📂 Moved", diff["moved"],
            lambda b: f"{_he(b)} (`{b['en']}`): `{b['old_category']}` → `{b['new_category']}`")
    section("📝 Content changed", diff["content_changed"], lambda b: f"{_he(b)}  (`{b['en']}`)")

    # New book versions are intentionally NOT written here — they are reported to the
    # forum (post_to_forum.py) via the JSON diff's "versions" key, not to a saved file.

    _write(args.out_md, "\n".join(lines))
    print(f"✅ Changelog written: {args.out_md} (added {n['added']}, removed {n['removed']}, "
          f"renamed {n['he_renamed'] + n['en_renamed']}, moved {n['moved']}, "
          f"content {n['content_changed']}, new versions {len(new_versions)})")
    return 0


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
