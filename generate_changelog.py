#!/usr/bin/env python3
"""Diff two export manifests into a Markdown changelog (+ optional --json diff for the forum)."""
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


def load_blacklist_keys(path):
    """Read books_blacklist.txt into a set of normalized keys (skips blanks and #-comments)."""
    keys = set()
    if not path or not os.path.isfile(path):
        return keys
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.lstrip("﻿").strip()
            if not line or line.startswith("#"):
                continue
            line = line.replace('\\"', '"').replace("\\'", "'")
            key = normalize_title_key(line)
            if key:
                keys.add(key)
    return keys


def load_titles(*paths):
    """Merge {English title: Hebrew title} maps; earlier paths win on conflicts."""
    merged = {}
    for path in reversed([p for p in paths if p]):
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                merged.update(data)
    return merged


def is_blacklisted(label, keys, titles):
    """A book/schema label is blacklisted when its English title (the last path segment)
    or its Hebrew title (looked up in titles) matches a blacklist key."""
    en = label.split("/")[-1]
    for candidate in (en, titles.get(en, "")):
        key = normalize_title_key(candidate)
        if key and key in keys:
            return True
    return False


def load_manifest(path):
    """Return {relative_path: sha256}. Tolerates a missing/empty file."""
    out = {}
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if len(line) < 67:  # 64 hash + 2 spaces + >=1 char path
                continue
            digest = line[:64]
            filepath = line[66:]  # skip the 64-char hash and the two spaces
            out[filepath] = digest
    return out


def classify(path):
    """Return (bucket, label): bucket is book|schema|link|toc|other."""
    p = path[2:] if path.startswith("./") else path
    if p.startswith("json/") and p.endswith("/merged.json"):
        return "book", p[len("json/"):-len("/merged.json")]
    if p.startswith("schemas/") and p.endswith(".json"):
        return "schema", p[len("schemas/"):-len(".json")]
    if p.startswith("links/"):
        return "link", p[len("links/"):]
    if p == "table_of_contents.json":
        return "toc", "table_of_contents.json"
    return "other", p


def bucketize(paths):
    """Group an iterable of paths by bucket -> sorted list of labels."""
    groups = {"book": [], "schema": [], "link": [], "toc": [], "other": []}
    for p in paths:
        bucket, label = classify(p)
        groups[bucket].append(label)
    for k in groups:
        groups[k].sort()
    return groups


def md_list(labels, limit=400):
    """Render labels as a Markdown bullet list, truncating very long lists."""
    lines = [f"- {lab}" for lab in labels[:limit]]
    if len(labels) > limit:
        lines.append(f"- …and {len(labels) - limit} more")
    return "\n".join(lines)


def section(title, groups, show=("book", "schema", "toc"), include_links=False):
    """Render one top-level section (Added / Removed / Changed)."""
    parts = []
    book_subtitles = {
        "book": "Books",
        "schema": "Schemas",
        "toc": "Table of contents",
        "link": "Link tables",
        "other": "Other files",
    }
    order = list(show)
    if include_links:
        order.append("link")
    order.append("other")
    for key in order:
        labels = groups.get(key, [])
        if not labels:
            continue
        parts.append(f"**{book_subtitles[key]}** ({len(labels)})\n\n{md_list(labels)}")
    return "\n\n".join(parts)


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
                    help="books_blacklist.txt; matching books/schemas are dropped from the output")
    ap.add_argument("--titles", default="",
                    help="current release titles.json (English->Hebrew, for blacklist matching)")
    ap.add_argument("--prev-titles", dest="prev_titles", default="",
                    help="previous release titles.json (Hebrew names for removed books)")
    args = ap.parse_args()

    old = load_manifest(args.old_manifest)
    new = load_manifest(args.new_manifest)

    if not new:
        print("❌ New manifest is empty — nothing to diff", file=sys.stderr)
        return 1

    new_keys, old_keys = set(new), set(old)
    added_paths = new_keys - old_keys
    removed_paths = old_keys - new_keys
    changed_paths = [k for k in (new_keys & old_keys) if old[k] != new[k]]

    added = bucketize(added_paths)
    removed = bucketize(removed_paths)
    changed = bucketize(changed_paths)

    # Drop blacklisted books (and their schemas) so books that never make it into the
    # library are excluded from both the release notes and the forum diff.
    blacklist_keys = load_blacklist_keys(args.blacklist)
    if blacklist_keys:
        titles = load_titles(args.titles, args.prev_titles)
        dropped = 0
        for groups in (added, removed, changed):
            for bucket in ("book", "schema"):
                kept = [lab for lab in groups[bucket]
                        if not is_blacklisted(lab, blacklist_keys, titles)]
                dropped += len(groups[bucket]) - len(kept)
                groups[bucket] = kept
        print(f"🚫 Blacklist ({len(blacklist_keys)} keys): dropped {dropped} "
              f"book/schema entr{'y' if dropped == 1 else 'ies'} from changelog & forum diff.")

    if args.json_out:
        diff = {
            "new_tag": args.new_tag,
            "old_tag": args.old_tag,
            "has_baseline": bool(old),
            "books": {
                "added": added["book"],
                "removed": removed["book"],
                "changed": changed["book"],
            },
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(diff, fh, ensure_ascii=False, indent=2)
        print(f"✅ Diff JSON written: {args.json_out}")

    n_books_added = len(added["book"])
    n_books_removed = len(removed["book"])
    n_books_changed = len(changed["book"])
    n_links_changed = len(changed["link"]) + len(added["link"]) + len(removed["link"])

    lines = []
    lines.append(f"## What changed in `{args.new_tag}`")
    lines.append("")
    if not old:
        lines.append(
            "_Initial release — no previous manifest to compare against. "
            "Future releases will list added / removed / changed books here._"
        )
        lines.append("")
        lines.append(f"- Total files in this release: **{len(new)}**")
        _write(args.out_md, "\n".join(lines))
        print(f"✅ Changelog written (initial): {args.out_md}")
        return 0

    base = f"`{args.old_tag}`" if args.old_tag else "the previous release"
    lines.append(f"Compared against {base}.")
    lines.append("")
    lines.append("| | Books | Schemas |")
    lines.append("|---|---:|---:|")
    lines.append(f"| ➕ Added | {n_books_added} | {len(added['schema'])} |")
    lines.append(f"| ➖ Removed | {n_books_removed} | {len(removed['schema'])} |")
    lines.append(f"| ✏️ Changed | {n_books_changed} | {len(changed['schema'])} |")
    lines.append("")
    note = []
    if n_links_changed:
        note.append(f"{n_links_changed} link table(s) regenerated")
    if changed["toc"] or added["toc"]:
        note.append("table of contents updated")
    if note:
        lines.append("_Also: " + ", ".join(note) + "._")
        lines.append("")

    if added_paths:
        body = section("Added", added)
        if body:
            lines.append("### ➕ Added")
            lines.append("")
            lines.append(body)
            lines.append("")
    if removed_paths:
        body = section("Removed", removed)
        if body:
            lines.append("### ➖ Removed")
            lines.append("")
            lines.append(body)
            lines.append("")
    if changed["book"] or changed["schema"]:
        body = section("Changed", changed)
        if body:
            lines.append("### ✏️ Changed (content)")
            lines.append("")
            lines.append(body)
            lines.append("")

    _write(args.out_md, "\n".join(lines))
    print(
        f"✅ Changelog written: {args.out_md} "
        f"(books +{n_books_added}/-{n_books_removed}/~{n_books_changed})"
    )
    return 0


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
