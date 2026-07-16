#!/usr/bin/env bash
set -euo pipefail

echo "Disabled: immutable release assets must be uploaded only by .github/workflows/release.yml." >&2
exit 2
