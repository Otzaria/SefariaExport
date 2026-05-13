#!/usr/bin/env python3
"""
Create the MongoDB indexes that the export pipeline depends on.

The `sefaria.export` module performs many `texts.find({title, language})`
queries sorted by `{priority:-1, _id:1}`. Without a covering index every
lookup degrades to a COLLSCAN of ~12k docs (~2.4 GB read), which turns the
full export into a multi-hour job on small runners (github-hosted = 2 vCPU,
slow virtualised disk).

This script is idempotent: existing indexes are left in place.
"""
import os
import sys

from pymongo import ASCENDING, DESCENDING, MongoClient


HOST = os.environ.get("MONGO_HOST", "127.0.0.1")
PORT = int(os.environ.get("MONGO_PORT", "27017"))
DB_NAME = os.environ.get("MONGO_DB_NAME", "sefaria")

INDEXES = {
    "texts": [
        # Covers the hot path `find({title, language}).sort({priority:-1, _id:1})`
        ([("title", ASCENDING), ("language", ASCENDING), ("priority", DESCENDING)], {}),
        ([("title", ASCENDING)], {}),
    ],
    "index": [
        ([("title", ASCENDING)], {}),
        ([("categories", ASCENDING)], {}),
    ],
    "term": [
        ([("name", ASCENDING)], {}),
    ],
}


def main() -> int:
    client = MongoClient(HOST, PORT, serverSelectionTimeoutMS=10_000)
    db = client[DB_NAME]
    for coll_name, specs in INDEXES.items():
        coll = db[coll_name]
        for keys, opts in specs:
            name = coll.create_index(keys, **opts)
            pretty = ", ".join(f"{k}:{d}" for k, d in keys)
            print(f"✅ {coll_name}: ensured index [{pretty}] -> {name}")
    print("✅ Critical indexes ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
