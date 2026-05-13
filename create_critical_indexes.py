#!/usr/bin/env python3
"""
Re-create all the MongoDB indexes Sefaria expects after a fresh restore.

Upstream Sefaria-Project ships the canonical list of indexes in
`sefaria.system.database.ensure_indices()` (≈80 specs covering texts,
links, index, term, vstate, history, …). The dump's metadata-only
restore leaves several of these out — most painfully `links.refs.0`,
without which `export_links()` does a 2.44 GB in-memory sort.

Calling the upstream helper keeps us in sync with whatever queries
Sefaria adds later without having to hand-maintain a parallel list.
"""
import os
import sys


def main() -> int:
    workspace = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    proj_dir = os.path.join(workspace, "Sefaria-Project")
    sys.path.insert(0, os.path.abspath(proj_dir))
    os.chdir(proj_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

    import django
    django.setup()

    from sefaria.system.database import ensure_indices

    print("🔧 Running sefaria.system.database.ensure_indices() ...")
    ensure_indices()
    print("✅ All Sefaria indexes ensured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
