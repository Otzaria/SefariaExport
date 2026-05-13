#!/usr/bin/env bash
set -euo pipefail

# Drop the sefaria database via pymongo (mongosh is not installed in this
# image — only mongodb-database-tools). Best-effort: any failure here is
# non-fatal since docker compose down --volumes cleans everything up.
python - <<'PY' || true
import os
from pymongo import MongoClient

host = os.environ.get("MONGO_HOST", "127.0.0.1")
port = int(os.environ.get("MONGO_PORT", "27017"))
name = os.environ.get("MONGO_DB_NAME", "sefaria")

client = MongoClient(host, port, serverSelectionTimeoutMS=5000)
client.drop_database(name)
print(f"✅ DB '{name}' dropped.")
PY

df -h
