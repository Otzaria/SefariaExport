#!/usr/bin/env python3
"""Build output/titles.json ({English title: Hebrew title}) from table_of_contents.json."""
import json
import os
import sys


def collect(node, out):
    """Recursively gather every {title -> heTitle} pair in the TOC tree."""
    if isinstance(node, dict):
        title = node.get("title")
        he = node.get("heTitle")
        if title and he:
            out[title] = he
        for v in node.values():
            collect(v, out)
    elif isinstance(node, list):
        for v in node:
            collect(v, out)


def main():
    exports_dir = os.environ.get(
        "SEFARIA_EXPORT_PATH",
        os.path.join(os.environ.get("GITHUB_WORKSPACE", os.getcwd()), "exports"),
    )
    out_dir = os.path.join(os.environ.get("GITHUB_WORKSPACE", os.getcwd()), "output")
    os.makedirs(out_dir, exist_ok=True)

    toc_path = os.path.join(exports_dir, "table_of_contents.json")
    if not os.path.isfile(toc_path):
        print(f"⚠️  No table_of_contents.json at {toc_path}; writing empty titles.json")
        titles = {}
    else:
        with open(toc_path, encoding="utf-8") as fh:
            toc = json.load(fh)
        titles = {}
        collect(toc, titles)

    out_path = os.path.join(out_dir, "titles.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(titles, fh, ensure_ascii=False, sort_keys=True)
    print(f"✅ titles.json written: {out_path} ({len(titles)} title→heTitle pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
