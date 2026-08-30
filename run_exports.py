#!/usr/bin/env python3
"""
Run a narrow Sefaria export tailored to what the SefariaSqlite generator
actually consumes:

  * Only the JSON merged format (drop txt / cltk-full / cltk-flat).
  * Only the Hebrew (`he`) language (skip the English merged pass entirely).
  * Plus links / schemas / TOC.

The cuts are applied at the source (Sefaria's `export_formats` tuple and a
custom `export_all_merged` loop), so we save both disk IO and CPU compared
to running the full upstream export.
"""
import os
import sys
import traceback
from pathlib import Path


def list_dir_limited(base: str) -> None:
    for root, dirs, files in os.walk(base):
        level = root.replace(base, '').count(os.sep)
        indent = ' ' * 2 * level
        print(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 2 * (level + 1)
        for file in files[:10]:
            print(f"{subindent}{file}")
        if len(files) > 10:
            print(f"{subindent}... and {len(files) - 10} more files")
        if level > 2:
            break


def run_merged_export_he_only(ex) -> None:
    """Replacement for `ex.export_all_merged()` — Hebrew only.

    Mirrors the upstream loop (see sefaria/export.py::export_all_merged) but
    drops the English pass to halve the number of slow Mongo lookups and
    skip writes we don't need.
    """
    from sefaria.system.database import db
    from sefaria.model.text import Ref

    titles = db.texts.find().distinct("title")
    total = len(titles)
    print(f"📋 {total} distinct titles to export (he only)")

    written = skipped = errored = 0
    for idx, title in enumerate(titles, 1):
        if not title:
            continue
        try:
            Ref(title)
        except Exception:
            skipped += 1
            continue

        if idx % 100 == 0 or idx == total:
            print(f"  …{idx}/{total} (written={written}, skipped={skipped}, errors={errored})", flush=True)

        try:
            prepped = ex.prepare_merged_text_for_export(title, lang="he")
            if prepped:
                ex.write_text_doc_to_disk(prepped)
                written += 1
        except Exception as e:  # pragma: no cover
            errored += 1
            print(f"⚠️  {title}: {e}", flush=True)

    print(f"✅ merged export done: written={written}, skipped={skipped}, errors={errored}")


def _version_text_is_empty(node) -> bool:
    """True when a version's text tree contains no non-whitespace string."""
    if node is None:
        return True
    if isinstance(node, str):
        return not node.strip()
    if isinstance(node, (list, tuple)):
        return all(_version_text_is_empty(child) for child in node)
    if isinstance(node, dict):
        return all(_version_text_is_empty(value) for value in node.values())
    return False


def run_versions_export_he_only(ex) -> None:
    """Per-version export for titles with 2+ Hebrew versions.

    merged.json is a per-segment mosaic decided by version `priority`; the
    individual editions (including ones fully shadowed by a higher-priority
    version) are invisible downstream. This pass writes every non-copyright
    Hebrew version of every multi-version title via the stock
    `prepare_text_for_export`, landing as `<versionTitle>.json` next to
    merged.json after `flatten_hebrew_dirs`. Single-version titles are
    skipped: their merged.json IS their only version.
    """
    from sefaria.system.database import db
    from sefaria.model.text import Ref

    counts = {}
    for doc in db.texts.find({"language": "he"}, {"title": 1, "license": 1}):
        title = doc.get("title")
        if title and not ex.text_is_copyright(doc):
            counts[title] = counts.get(title, 0) + 1
    multi = sorted(t for t, n in counts.items() if n > 1)
    total = sum(counts[t] for t in multi)
    print(f"📋 {len(multi)} titles with 2+ non-copyright Hebrew versions "
          f"(of {len(counts)} titles) → {total} version docs to export")

    written = empty = skipped = errored = 0
    seen_filenames = set()
    for text in db.texts.find({"language": "he", "title": {"$in": multi}}):
        title = text.get("title")
        if not title or ex.text_is_copyright(text):
            continue
        version_title = text.get("versionTitle")
        if not isinstance(version_title, str) or not version_title.strip():
            skipped += 1
            print(f"⚠️  {title}: version without versionTitle skipped", flush=True)
            continue
        try:
            Ref(title)
        except Exception:
            skipped += 1
            continue

        filename = ex.remove_illegal_file_chars(version_title)
        # A version sanitizing to "merged" would land on merged.json (and the
        # SefariaSqlite generator matches that name case-insensitively); two
        # versions collapsing to one filename would overwrite each other.
        # Both silently corrupt data — abort the run instead.
        if not filename or filename.lower() == "merged":
            raise RuntimeError(
                f"version filename collides with merged.json: {title} / {version_title!r}")
        key = (title, filename.lower())
        if key in seen_filenames:
            raise RuntimeError(
                f"two versions collapse to the same filename: {title} / {version_title!r}")
        seen_filenames.add(key)

        if _version_text_is_empty(text.get("chapter")):
            empty += 1
            continue

        try:
            prepped = ex.prepare_text_for_export(text)
            if prepped:
                ex.write_text_doc_to_disk(prepped)
                written += 1
                if written % 100 == 0:
                    print(f"  …{written}/{total} (empty={empty}, skipped={skipped}, "
                          f"errors={errored})", flush=True)
        except Exception as e:  # pragma: no cover
            errored += 1
            print(f"⚠️  {title} / {version_title}: {e}", flush=True)

    print(f"✅ versions export done: written={written}, empty={empty}, "
          f"skipped={skipped}, errors={errored}")


# Link-visibility bits. Mirrors the three display filters Sefaria applies in
# `get_links()` (sefaria/client/wrapper.py). A side with mask 0 is displayed.
SUPPRESS_ANCHOR_NOT_SEGMENT = 1
SUPPRESS_OTHER_TOO_COARSE = 2
SUPPRESS_WHOLE_PEREK = 4
SUPPRESS_WHOLE_PARASHA = 8

SUPPRESSION_BITS = {
    SUPPRESS_ANCHOR_NOT_SEGMENT: "anchor_not_segment_level",
    SUPPRESS_OTHER_TOO_COARSE: "other_side_too_coarse",
    SUPPRESS_WHOLE_PEREK: "whole_talmud_perek",
    SUPPRESS_WHOLE_PARASHA: "whole_parasha",
}


def _node_depth(oref):
    """`index_node.depth`, or None when the node can't supply one."""
    try:
        return getattr(oref.index_node, "depth", None)
    except Exception:
        return None


def _side_mask(anchor, other, anchor_ref, perek_refs, parasha_refs) -> int:
    """Why Sefaria would refuse to surface this link on `anchor`'s side.

    Each bit is one `continue` in get_links(). Note the second filter measures
    the OTHER side against the OTHER side's own depth, not the anchor's.
    """
    mask = 0
    depth = _node_depth(anchor)
    if depth is None or len(anchor.sections) != depth:
        mask |= SUPPRESS_ANCHOR_NOT_SEGMENT
    other_depth = _node_depth(other)
    if other_depth is None or len(other.sections) + 1 < other_depth:
        mask |= SUPPRESS_OTHER_TOO_COARSE
    if anchor_ref in perek_refs:
        mask |= SUPPRESS_WHOLE_PEREK
    if anchor_ref in parasha_refs:
        mask |= SUPPRESS_WHOLE_PARASHA
    return mask


def _sefaria_project_sha(project_dir=None) -> str:
    """The exact Sefaria-Project checkout whose helpers produced the masks."""
    import subprocess

    # The Docker entrypoint clones Sefaria beside this script under
    # /app/Sefaria-Project; the process itself runs from /app, which is not a
    # Git checkout. Never derive provenance from the caller's working dir.
    cwd = project_dir or Path(__file__).resolve().parent / "Sefaria-Project"
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot resolve Sefaria-Project commit at {cwd}: {exc}") from exc
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"invalid Sefaria-Project commit returned by git: {sha!r}")
    return sha


def run_links_export_extended() -> None:
    """Replacement for `ex.export_links()` — adds word-level anchor fields.

    Mirrors the upstream loop (see sefaria/export.py::export_links) — same
    file naming, chunking, column order and aggregate files — but appends
    two columns the upstream export drops on the floor:

      * `Char Level Data 1/2` — JSON dict per ref side with
        startChar/endChar (or startWord/endWord for Tanakh verses) plus the
        versionTitle+language the offsets were computed against
        (mongo `links.charLevelData`).

    (`highlightedWords` is intentionally NOT exported: the 2026-07-02 run
    showed zero populated documents, so the column would be dead weight.)

    Consumers that index columns by header name are unaffected by the
    trailing additions.
    """
    import hashlib
    import json
    import unicodecsv as csv
    from collections import Counter

    from sefaria.helper.text import get_parasha_ref_set, get_talmud_perek_ref_set
    from sefaria.model.text import Ref
    from sefaria.system.database import db
    from sefaria.system.exceptions import InputError

    # Authoritative sets, straight from Sefaria's own helpers. Hoisted out of
    # the row loop: they are lru_cached but this runs millions of times.
    perek_refs = get_talmud_perek_ref_set()
    parasha_refs = get_parasha_ref_set()
    print(f"Link visibility: {len(perek_refs)} perek refs, {len(parasha_refs)} parasha refs")
    suppressed_by_side_and_bit = Counter()
    suppressed_sides = Counter()

    export_base = os.environ["SEFARIA_EXPORT_PATH"]

    print("Exporting links (extended)...")
    links_by_book = Counter()
    links_by_book_without_commentary = Counter()
    field_counts = Counter()

    path = os.path.join(export_base, "links")
    os.makedirs(path, exist_ok=True)

    def dumps(value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    link_file_number = 0
    csvfile = None
    writer = None
    links = db.links.find().sort([["refs.0", 1]])
    new_links_file_size = 300000
    for i, link in enumerate(links):
        if i % new_links_file_size == 0:
            filename = os.path.join(path, f"links{link_file_number}.csv")
            if csvfile is not None:
                csvfile.close()
            csvfile = open(filename, 'wb')
            writer = csv.writer(csvfile)
            writer.writerow([
                    "Citation 1",
                    "Citation 2",
                    "Conection Type",
                    "Text 1",
                    "Text 2",
                    "Category 1",
                    "Category 2",
                    "Char Level Data 1",
                    "Char Level Data 2",
                    "Suppression Mask 1",
                    "Suppression Mask 2",
            ])
            link_file_number += 1

        # A malformed link document (missing/short refs) must not kill a
        # 50-minute export run — skip it, but keep it visible in the summary.
        refs = link.get("refs")
        if not isinstance(refs, list) or len(refs) < 2:
            field_counts["refs_malformed"] += 1
            print(f"⚠️  malformed refs on link {link.get('_id')}: {refs!r}")
            continue

        try:
            oref1 = Ref(refs[0])
            oref2 = Ref(refs[1])
        except InputError:
            continue

        char_level = link.get("charLevelData")
        char_cells = ["", ""]
        if isinstance(char_level, list) and len(char_level) == 2:
            char_cells = [dumps(char_level[0]), dumps(char_level[1])]
            field_counts["charLevelData"] += 1
        elif char_level is not None:
            field_counts["charLevelData_malformed"] += 1
            print(f"⚠️  malformed charLevelData on {link['refs']}: {char_level!r}")

        # Per-side visibility, decided here because this is the only place the
        # TermSet, index_node depths and both Refs exist together.
        mask1 = _side_mask(oref1, oref2, refs[0], perek_refs, parasha_refs)
        mask2 = _side_mask(oref2, oref1, refs[1], perek_refs, parasha_refs)
        for side, mask in ((1, mask1), (2, mask2)):
            if mask:
                suppressed_sides[side] += 1
                for bit, name in SUPPRESSION_BITS.items():
                    if mask & bit:
                        suppressed_by_side_and_bit[(side, name)] += 1

        link_type = link.get("type", "")
        writer.writerow([
            refs[0],
            refs[1],
            link_type,
            oref1.book,
            oref2.book,
            oref1.index.categories[0],
            oref2.index.categories[0],
            char_cells[0],
            char_cells[1],
            mask1,
            mask2,
        ])

        book_link = tuple(sorted([oref1.index.title, oref2.index.title]))
        links_by_book[book_link] += 1
        if link_type not in ("commentary", "Commentary", "targum", "Targum"):
            links_by_book_without_commentary[book_link] += 1

    if csvfile is not None:
        csvfile.close()

    def write_aggregate_file(counter, filename):
        with open(os.path.join(path, filename), 'wb') as aggfile:
            agg_writer = csv.writer(aggfile)
            agg_writer.writerow([
                "Text 1",
                "Text 2",
                "Link Count",
            ])
            for link in counter.most_common():
                agg_writer.writerow([
                    link[0][0],
                    link[0][1],
                    link[1],
                ])

    write_aggregate_file(links_by_book, "links_by_book.csv")
    write_aggregate_file(links_by_book_without_commentary, "links_by_book_without_commentary.csv")

    # QA sidecar. NOT the source of the decision — that ships per row above —
    # but it lets a consumer verify its own derivation against Sefaria's sets.
    def digest(refs_set) -> str:
        return hashlib.sha256("\n".join(sorted(refs_set)).encode("utf-8")).hexdigest()

    meta_dir = os.path.join(export_base, "metadata")
    os.makedirs(meta_dir, exist_ok=True)
    sefaria_project_sha = _sefaria_project_sha()
    visibility = {
        "schema_version": 1,
        "sefaria_project_sha": sefaria_project_sha,
        "mask_bits": {str(bit): name for bit, name in SUPPRESSION_BITS.items()},
        "counts": {
            "perek_refs": len(perek_refs),
            "parasha_refs": len(parasha_refs),
            "suppressed_side_1": suppressed_sides[1],
            "suppressed_side_2": suppressed_sides[2],
            "suppressed_by_side_and_bit": {
                str(side): {
                    name: suppressed_by_side_and_bit[(side, name)]
                    for name in sorted(SUPPRESSION_BITS.values())
                }
                for side in (1, 2)
            },
        },
        "perek_refs_sha256": digest(perek_refs),
        "parasha_refs_sha256": digest(parasha_refs),
        "perek_refs": sorted(perek_refs),
        "parasha_refs": sorted(parasha_refs),
    }
    with open(os.path.join(meta_dir, "link-visibility-v1.json"), "w", encoding="utf-8") as vf:
        json.dump(visibility, vf, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"✅ links export done: charLevelData={field_counts['charLevelData']}, "
          f"malformed={field_counts['charLevelData_malformed'] + field_counts['refs_malformed']}")
    print(f"   visibility: sides suppressed 1={suppressed_sides[1]} 2={suppressed_sides[2]}, "
          f"by side/bit={dict(sorted(suppressed_by_side_and_bit.items()))}, "
          f"sefaria={sefaria_project_sha[:12]}")


def flatten_hebrew_dirs(export_base: str) -> None:
    """Move the contents of every `.../Hebrew/` directory one level up.

    Sefaria's `make_path` writes to `json/<cat>/<book>/Hebrew/merged.json`.
    The SefariaSqlite generator expects `json/<cat>/<book>/merged.json`, so
    we collapse the language layer in-place.
    """
    import shutil

    targets = []
    for root, dirs, _files in os.walk(export_base):
        for d in dirs:
            if d == "Hebrew":
                targets.append(os.path.join(root, d))

    print(f"📦 Flattening {len(targets)} Hebrew/ directories under {export_base}")
    for src in targets:
        parent = os.path.dirname(src)
        for entry in os.listdir(src):
            shutil.move(os.path.join(src, entry), os.path.join(parent, entry))
        try:
            os.rmdir(src)
        except OSError:
            pass


def main() -> int:
    workspace = os.environ.get('GITHUB_WORKSPACE', os.getcwd())
    proj_dir = os.path.join(workspace, 'Sefaria-Project')
    sys.path.insert(0, os.path.abspath(proj_dir))
    os.chdir(proj_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

    export_base = os.path.join(workspace, 'exports')
    os.environ["SEFARIA_EXPORT_PATH"] = export_base

    print(f"📁 Export base directory: {export_base}")
    print(f"📁 Current working directory: {os.getcwd()}")

    import django
    django.setup()

    from django.conf import settings
    print(f"📋 Django SEFARIA_EXPORT_PATH: {getattr(settings, 'SEFARIA_EXPORT_PATH', 'NOT SET')}")

    from sefaria import export as ex

    # Drop txt / cltk-full / cltk-flat formats at the source. This also
    # saves the CPU spent by make_cltk_* on every book.
    print(f"🪓 Restricting export_formats from {[f[0] for f in ex.export_formats]} -> ['json']")
    ex.export_formats = (('json', ex.make_json),)

    try:
        print("\n" + "="*60)
        print("▶️  Running merged export (Hebrew + JSON only)")
        print("="*60)
        run_merged_export_he_only(ex)

        print(f"\n{'='*60}\n▶️  Running versions export (multi-version titles, Hebrew + JSON only)\n{'='*60}")
        run_versions_export_he_only(ex)

        print(f"\n{'='*60}\n▶️  Running export_links (extended)...\n{'='*60}")
        run_links_export_extended()
        print("✅ export_links (extended) completed")

        for fn_name in ("export_schemas", "export_toc"):
            print(f"\n{'='*60}\n▶️  Running {fn_name}...\n{'='*60}")
            getattr(ex, fn_name)()
            print(f"✅ {fn_name} completed")
    except Exception as e:  # pragma: no cover
        print(f"❌ export step failed: {e}")
        traceback.print_exc()
        return 1

    # Collapse `json/<cat>/<book>/Hebrew/` -> `json/<cat>/<book>/` to match
    # the layout the SefariaSqlite generator expects.
    flatten_hebrew_dirs(export_base)

    print(f"\n📂 Final layout of {export_base}:")
    if os.path.isdir(export_base):
        list_dir_limited(export_base)

    print("\n✅ All exports completed successfully")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
