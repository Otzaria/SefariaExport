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

The blacklist-filtered display run also lists, under books.previously_blocked, books that
SeforimLibrary would import now but that its current blacklists block under their previous
title or path — they reach the library as new books.
"""
import argparse
import json
import os
import re
import sys
from collections import namedtuple

# Java's \s, which SeforimLibrary's normalizeTitleKey uses, is ASCII-only.
_JAVA_WHITESPACE = re.compile(r"[ \t\n\x0b\f\r]+")


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
    collapsed = _JAVA_WHITESPACE.sub(" ", without_quotes.lower()).replace("_", " ")
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


def sanitize_folder(name):
    """SeforimLibrary's sanitizeFolder: ASCII double quote -> gershayim, trimmed."""
    if name is None or not name.strip():
        return ""
    return name.replace('"', "״").strip()


def flatten_talmud(parts):
    """SeforimLibrary's flattenTalmudCategories: 'תלמוד', 'בבלי' -> 'תלמוד בבלי'."""
    out, i = [], 0
    while i < len(parts):
        if parts[i] == "תלמוד" and i + 1 < len(parts) and parts[i + 1] in ("בבלי", "ירושלמי"):
            out.append(f"תלמוד {parts[i + 1]}")
            i += 2
        else:
            out.append(parts[i])
            i += 1
    return out


def normalize_path_entry(raw):
    """SeforimLibrary's normalizePriorityEntry, applied to books_blacklist path lines."""
    entry = raw.strip().replace("\\", "/")
    if entry.startswith("/"):
        entry = entry[1:]
    parts = [sanitize_folder(p) for p in entry.split("/") if p.strip()]
    return "/".join(flatten_talmud(parts))


def book_path(he_categories, he_title):
    """SeforimLibrary's normalizedBookPath over its flattened Hebrew categories."""
    categories = flatten_talmud([sanitize_folder(c) for c in he_categories])
    return "/".join([sanitize_folder(c) for c in categories] + [sanitize_folder(he_title)])


# How SeforimLibrary sees one book: titles and authors to match; he_categories is None
# when the export's schemas were not read (title-only matching).
Identity = namedtuple("Identity", "titles he_categories he_title authors")


class Blacklists:
    """books_blacklist (title and path lines) + authors_blacklist, as SefariaBlacklists."""

    def __init__(self, books_path="", authors_path=""):
        books = list(blacklist_lines(books_path))
        self.titles = {key for line in books if (key := normalize_title_key(line))}
        self.paths = {path for line in books
                      if ("/" in line or "\\" in line) and (path := normalize_path_entry(line))}
        self.authors = {key for line in blacklist_lines(authors_path)
                        if (key := normalize_title_key(line))}

    def __bool__(self):
        return bool(self.titles or self.paths or self.authors)

    def title_match(self, *titles):
        """The first title whose key is blacklisted, else None."""
        return next((t for t in titles if normalize_title_key(t) in self.titles), None)

    def blocks(self, identity):
        if self.title_match(*identity.titles):
            return True
        if self.paths and identity.he_categories is not None \
                and book_path(identity.he_categories, identity.he_title) in self.paths:
            return True
        return any(normalize_title_key(a) in self.authors for a in identity.authors)


def load_author_forms(path):
    """authors.json -> {slug: [Hebrew name forms]}, as SefariaAuthorTitles.load."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list):
        raise ValueError(f"{path} is not a JSON array")
    forms = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"{path} entry {index} is not an object")
        slug = record.get("slug")
        slug = slug.strip() if isinstance(slug, str) else ""
        if not slug:
            continue
        titles = record.get("titles") if isinstance(record.get("titles"), list) else []
        hebrew = []
        for title in titles:
            if isinstance(title, dict) and title.get("lang") == "he" and isinstance(title.get("text"), str):
                text = title["text"].strip()
                if text and text not in hebrew:
                    hebrew.append(text)
        if not hebrew:
            continue
        if slug in forms:
            raise ValueError(f"{path} lists slug {slug!r} more than once")
        forms[slug] = hebrew
    return forms


class NotImported(Exception):
    """SeforimLibrary's parseBookFile would return null: the book is never imported."""


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def string_or_none(value):
    """Kotlin stringOrNull: a JSON primitive's content; None for null, objects and arrays."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return None


def first_not_none(*values):
    """Kotlin's `?:` chain: an empty string is a value, only null falls through."""
    return next((v for v in values if v is not None), None)


def json_array(obj, key):
    """Kotlin obj[key]?.jsonArray: None when absent; a non-array value throws."""
    if key not in obj:
        return None
    if not isinstance(obj[key], list):
        raise NotImported(f"{key} is not an array")
    return obj[key]


def primitive_contents(items):
    """Kotlin mapNotNull { it.jsonPrimitive.contentOrNull }."""
    if any(isinstance(item, (dict, list)) for item in items):
        raise NotImported("non-primitive array element")
    return [c for item in items if (c := string_or_none(item)) is not None]


class ExportIdentities:
    """Current books as SeforimLibrary's parseBookFile reads them; None = not imported."""

    def __init__(self, exports_dir, labels, author_forms):
        self.exports_dir = exports_dir
        self.schema_dir = os.path.join(exports_dir, "schemas")
        self.labels = labels
        self.author_forms = author_forms
        self.lookup = None
        self.cache = {}

    def __call__(self, en):
        if en not in self.cache:
            try:
                self.cache[en] = self._read(en)
            except NotImported:
                self.cache[en] = None
        return self.cache[en]

    def not_imported(self):
        return sorted(en for en, identity in self.cache.items() if identity is None)

    def _schema_lookup(self):
        """buildSchemaLookup; first file wins, in sorted order (Kotlin's is directory order)."""
        if self.lookup is None:
            self.lookup = {}
            for name in sorted(os.listdir(self.schema_dir)):
                if not name.endswith(".json"):
                    continue
                doc = read_json(os.path.join(self.schema_dir, name))
                node = doc.get("schema") if isinstance(doc, dict) else None
                if not isinstance(node, dict):
                    continue
                for title in (node.get("title"), node.get("heTitle")):
                    if key := normalize_title_key(string_or_none(title)):
                        self.lookup.setdefault(key, os.path.join(self.schema_dir, name))
        return self.lookup

    def _resolve_schema(self, title, he_title, folder):
        """resolveSchemaPath: normalized lookup first, then <candidate>.json, per candidate."""
        for candidate in (c for c in (title, he_title, folder.replace("_", " "), folder) if c is not None):
            key = normalize_title_key(candidate)
            if key and key in self._schema_lookup():
                return self.lookup[key]
            path = os.path.join(self.schema_dir, candidate.replace(" ", "_") + ".json")
            if os.path.exists(path):
                return path
        return None

    def _read(self, en):
        label = self.labels.get(en)
        merged = read_json(os.path.join(self.exports_dir, "json", f"{label}/merged.json")) if label else None
        if not isinstance(merged, dict):
            raise NotImported("merged.json unreadable")
        file_title, file_he = string_or_none(merged.get("title")), string_or_none(merged.get("heTitle"))
        schema_path = self._resolve_schema(file_title, file_he, en)
        doc = read_json(schema_path) if schema_path else None
        node = doc.get("schema") if isinstance(doc, dict) else None
        if not isinstance(node, dict):
            raise NotImported("no schema")
        en_title = first_not_none(string_or_none(node.get("title")), file_title, en)
        he_title = first_not_none(string_or_none(node.get("heTitle")), file_he, en_title)
        if "text" not in merged:
            raise NotImported("no text")
        he_categories = json_array(doc, "heCategories")
        if he_categories is None:
            he_categories = json_array(node, "heCategories")
        if he_categories is None:
            he_categories = json_array(merged, "categories")
        authors = []
        for entry in json_array(doc, "authors") or []:
            if not isinstance(entry, dict):
                raise NotImported("author entry is not an object")
            if (he := string_or_none(entry.get("he"))) is not None:
                authors.append(he)
                slug = string_or_none(entry.get("slug"))
                authors += self.author_forms.get(slug.strip(), []) if slug is not None else []
        return Identity((en_title, he_title), primitive_contents(he_categories or []),
                        he_title, tuple(authors))


def load_title_map(path):
    """Load one {English title: Hebrew title} map; tolerates a missing/empty file."""
    if path and os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    return {}


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


def diff_versions(old, new, new_titles, exports_dir, book_blocked, vbl):
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
        if book_blocked(book_en):
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


def excluded(bl, current, en):
    """SeforimLibrary skips the book: not importable, or blacklisted."""
    identity = current(en)
    return identity is None or bl.blocks(identity)


def previously_blocked(bl, current, prior):
    """Books importable now that the current blacklists block under their previous title
    or path. prior: {en: (old_en, old_he, new_he, same_category)}. A path entry whose
    leaf is the old title, on a book whose old categories are unknown, lists it too."""
    # normalizedBookPath ends with the sanitized heTitle, so only that leaf can match.
    leaves = {path.rsplit("/", 1)[-1] for path in bl.paths}
    out = []
    for en in sorted(prior):
        old_en, old_he, new_he, same_category = prior[en]
        matched = bl.title_match(old_he, old_en)
        leaf_hit = sanitize_folder(old_he if old_he is not None else old_en) in leaves
        if not (matched or leaf_hit) or excluded(bl, current, en):
            continue
        entry = {"en": en, "he": new_he, "old_en": old_en, "old_he": old_he}
        if matched:
            out.append({**entry, "reason": "title", "old_name": matched})
            continue
        he_categories = current(en).he_categories
        if not (same_category and old_he is not None and he_categories is not None):
            out.append({**entry, "reason": "path-unknown", "old_name": None})
        elif (old_path := book_path(he_categories, old_he)) in bl.paths:
            out.append({**entry, "reason": "path", "old_name": old_path})
    return out


def apply_blacklist(diff, bl, current, prior):
    """Drop books SeforimLibrary would skip and move books blocked only under their previous
    identity to diff["previously_blocked"]; return the count dropped."""
    if not bl:
        return 0
    escaped = previously_blocked(bl, current, prior)
    gone = {b["en"] for b in escaped}
    dropped = 0

    def keep(en, blocked):
        nonlocal dropped
        if blocked:
            dropped += 1
            return False
        return en not in gone

    def keep_current(en):
        return keep(en, excluded(bl, current, en))

    diff["added"] = [b for b in diff["added"] if keep_current(b["en"])]
    # A removed book has no schema in this export: its titles are all there is to match.
    diff["removed"] = [b for b in diff["removed"] if keep(b["en"], bl.title_match(b["en"], b["he"]))]
    diff["content_changed"] = [b for b in diff["content_changed"] if keep_current(b["en"])]
    diff["moved"] = [b for b in diff["moved"] if keep_current(b["en"])]
    diff["he_renamed"] = [b for b in diff["he_renamed"] if keep_current(b["en"])]
    diff["en_renamed"] = [b for b in diff["en_renamed"] if keep_current(b["new_en"])]
    diff["previously_blocked"] = escaped
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
    ap.add_argument("--authors-blacklist", dest="authors_blacklist", default="",
                    help="authors_blacklist.txt; books by a matching author are dropped")
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

    old_recs, new_recs = book_records(old), book_records(new)
    bl = Blacklists(args.blacklist, args.authors_blacklist)
    identities = None
    if bl and args.exports_dir and os.path.isdir(os.path.join(args.exports_dir, "schemas")):
        try:
            author_forms = load_author_forms(os.path.join(args.exports_dir, "authors.json"))
        except (OSError, ValueError) as exc:
            print(f"::warning::authors.json is unreadable ({exc}) — author blacklist NOT applied")
            author_forms, bl.authors = {}, set()
        identities = ExportIdentities(args.exports_dir,
                                      {en: r["label"] for en, r in new_recs.items()}, author_forms)
        current = identities
    else:
        if bl.paths or bl.authors:
            print("::warning::No export schemas to read — path and author blacklists NOT applied")
            bl.paths, bl.authors = set(), set()

        def current(en):
            return Identity((en, new_titles.get(en)), None, None, ())

    diff = diff_books(old_recs, new_recs, old_titles, new_titles)
    pairs = {en: en for en in set(old_recs) & set(new_recs)}
    pairs.update({b["new_en"]: b["old_en"] for b in diff["en_renamed"]})
    prior = {en: (old_en, old_titles.get(old_en), new_titles.get(en),
                  old_recs[old_en]["category"] == new_recs[en]["category"])
             for en, old_en in pairs.items()}
    dropped = apply_blacklist(diff, bl, current, prior)
    if dropped:
        print(f"🚫 Blacklist: dropped {dropped} book entr{'y' if dropped == 1 else 'ies'} "
              f"from changelog & forum diff.")

    # New book editions (book_version) — reported so they can be triaged into
    # black_versions.txt. Exact versionTitle is read from the export files.
    # Skipped on an initial release (no baseline → every version looks "new").
    versions_blacklist = load_versions_blacklist(args.versions_blacklist)
    new_versions = diff_versions(
        old, new, new_titles, args.exports_dir,
        lambda en: bool(bl) and excluded(bl, current, en), versions_blacklist,
    ) if old else []
    if identities and (skipped := identities.not_imported()):
        print(f"::warning::{len(skipped)} book(s) SeforimLibrary would not import (no usable "
              f"merged.json/schema) — dropped from the forum copy: {', '.join(skipped[:20])}")
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
        *([f"| 🔓 No longer blacklisted | {n['previously_blocked']} |"]
          if "previously_blocked" in n else []),
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
            "_New books and new versions are announced on the Otzaria forum; the full "
            "per-book list ships as the `CHANGELOG.md` asset of this release._",
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
    section("🔓 No longer blacklisted (blocked under a previous title/path)",
            diff.get("previously_blocked", []),
            lambda b: f"{_he(b)}  (`{b['en']}`): {b['reason']} `{b['old_name'] or '?'}`")

    # New book versions are intentionally NOT written here — they are reported to the
    # forum (post_to_forum.py) via the JSON diff's "versions" key, not to a saved file.

    _write(args.out_md, "\n".join(lines))
    print(f"✅ Changelog written: {args.out_md} (added {n['added']}, removed {n['removed']}, "
          f"renamed {n['he_renamed'] + n['en_renamed']}, moved {n['moved']}, "
          f"content {n['content_changed']}, new versions {len(new_versions)}"
          + (f", no longer blacklisted {n['previously_blocked']}" if "previously_blocked" in n else "")
          + ")")
    return 0


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
