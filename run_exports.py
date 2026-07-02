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


def run_links_export_extended() -> None:
    """Replacement for `ex.export_links()` — adds word-level anchor fields.

    Mirrors the upstream loop (see sefaria/export.py::export_links) — same
    file naming, chunking, column order and aggregate files — but appends
    three columns the upstream export drops on the floor:

      * `Highlighted Words`  — JSON list of quoted words to highlight
        (mongo `links.highlightedWords`, set by the quotation finder).
      * `Char Level Data 1/2` — JSON dict per ref side with
        startChar/endChar (or startWord/endWord for Tanakh verses) plus the
        versionTitle+language the offsets were computed against
        (mongo `links.charLevelData`).

    Consumers that index columns by header name are unaffected by the
    trailing additions.
    """
    import json
    import unicodecsv as csv
    from collections import Counter

    from sefaria.model.text import Ref
    from sefaria.system.database import db
    from sefaria.system.exceptions import InputError

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
                    "Highlighted Words",
                    "Char Level Data 1",
                    "Char Level Data 2",
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

        highlighted = link.get("highlightedWords")
        highlighted_cell = ""
        if isinstance(highlighted, list) and highlighted:
            highlighted_cell = dumps(highlighted)
            field_counts["highlightedWords"] += 1
        elif highlighted not in (None, []):
            field_counts["highlightedWords_malformed"] += 1
            print(f"⚠️  malformed highlightedWords on {link['refs']}: {highlighted!r}")

        char_level = link.get("charLevelData")
        char_cells = ["", ""]
        if isinstance(char_level, list) and len(char_level) == 2:
            char_cells = [dumps(char_level[0]), dumps(char_level[1])]
            field_counts["charLevelData"] += 1
        elif char_level is not None:
            field_counts["charLevelData_malformed"] += 1
            print(f"⚠️  malformed charLevelData on {link['refs']}: {char_level!r}")

        link_type = link.get("type", "")
        writer.writerow([
            refs[0],
            refs[1],
            link_type,
            oref1.book,
            oref2.book,
            oref1.index.categories[0],
            oref2.index.categories[0],
            highlighted_cell,
            char_cells[0],
            char_cells[1],
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

    print(f"✅ links export done: highlightedWords={field_counts['highlightedWords']}, "
          f"charLevelData={field_counts['charLevelData']}, "
          f"malformed={field_counts['highlightedWords_malformed'] + field_counts['charLevelData_malformed'] + field_counts['refs_malformed']}")


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
