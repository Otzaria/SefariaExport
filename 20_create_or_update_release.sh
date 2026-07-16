#!/usr/bin/env bash
set -euo pipefail

echo "Disabled: immutable releases must be created only by .github/workflows/release.yml." >&2
exit 2
