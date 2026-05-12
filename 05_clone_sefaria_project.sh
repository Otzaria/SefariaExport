#!/usr/bin/env bash
set -euo pipefail

if [ -d "Sefaria-Project" ]; then
  echo "Sefaria-Project already exists, skipping clone"
else
  git clone --depth 1 https://github.com/Sefaria/Sefaria-Project.git
fi

# Relax upstream pins that drop Python 3.9 support.
# tinycss2 1.5.x requires Python >=3.10; downgrade to the last 3.9-compatible
# release. Add further sed lines here if other deps follow the same pattern.
REQ_FILE="Sefaria-Project/requirements.txt"
if [ -f "$REQ_FILE" ]; then
  sed -i -E 's/^tinycss2==1\.5\.[0-9]+$/tinycss2==1.4.0/' "$REQ_FILE"
fi

ls -la Sefaria-Project | head -n 50
