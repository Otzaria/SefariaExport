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

The helper logs and swallows individual index creation failures. Verify the
actual links export plan afterwards, before its millions of rows are exported.
"""
import os
import sys
import time


def _plan_nodes(value):
    """Walk the winning logical plan, including classic and SBE wrappers.

    Rejected alternatives and SBE's internal text plan are not the chosen
    logical tree. In particular, a rejected SORT must not fail a valid IXSCAN.
    """
    if isinstance(value, dict):
        if isinstance(value.get("stage"), str):
            yield value
        for key, child in value.items():
            if key not in ("rejectedPlans", "slotBasedPlan"):
                yield from _plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _plan_nodes(child)


def verify_links_query_plan(db) -> dict:
    """Reject a blocking sort for the exporter's exact find/sort shape.

    queryPlanner does not execute the export query to completion. Avoid
    Cursor.explain(), whose driver default can request execution statistics.
    No hint is used: reverse and compound indexes are valid if the planner
    can obtain the requested order from them.
    """
    explained = db.command({
        "explain": {
            "find": "links",
            "filter": {},
            "sort": {"refs.0": 1},
        },
        "verbosity": "queryPlanner",
        # MongoDB 6 ignores maxTimeMS inside the explained find command.
        # Apply the deadline to the outer explain command instead.
        "maxTimeMS": 10000,
    })
    planner = explained.get("queryPlanner") if isinstance(explained, dict) else None
    winning = planner.get("winningPlan") if isinstance(planner, dict) else None
    nodes = list(_plan_nodes(winning))
    stages = [node["stage"] for node in nodes]
    if not stages:
        raise RuntimeError(
            "Cannot verify the links export query plan: no winning stages "
            "in MongoDB explain output. Refusing to continue unchecked."
        )
    if "SORT" in stages:
        raise RuntimeError(
            "The links export still requires a blocking SORT on refs.0 after "
            "ensure_indices(). Check the index creation errors above and the "
            "usable refs.0 indexes; refusing to export with an unindexed sort."
        )
    return {
        "stages": stages,
        "indexes": list(dict.fromkeys(
            node["indexName"] for node in nodes if "indexName" in node
        )),
    }


def ensure_and_verify_indexes(ensure_indices, db) -> None:
    # The upstream pass may take minutes and exposes no progress callback.
    print("🔧 Running sefaria.system.database.ensure_indices() "
          "(~6-7 min, no output until it finishes) ...", flush=True)
    started = time.monotonic()
    ensure_indices()
    elapsed = time.monotonic() - started
    # Returning is not evidence that every index was created: upstream catches
    # OperationFailure. Only the critical links plan is verified below.
    print(f"Index creation pass returned after {elapsed:.0f}s; "
          "checking the links export query plan...", flush=True)
    started = time.monotonic()
    plan = verify_links_query_plan(db)
    elapsed = time.monotonic() - started
    print(f"✅ Links export plan verified in {elapsed:.3f}s: "
          f"stages={','.join(plan['stages'])}; "
          f"indexes={','.join(plan['indexes']) or '(none)'}", flush=True)


def main() -> int:
    workspace = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    proj_dir = os.path.join(workspace, "Sefaria-Project")
    sys.path.insert(0, os.path.abspath(proj_dir))
    os.chdir(proj_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

    import django
    django.setup()

    from sefaria.system.database import db, ensure_indices

    ensure_and_verify_indexes(ensure_indices, db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
