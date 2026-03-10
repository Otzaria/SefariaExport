#!/usr/bin/env bash
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      SUDO="sudo"
    else
      echo "❌ sudo is required to install build dependencies" >&2
      exit 1
    fi
  fi
  $SUDO apt-get update -y
  $SUDO apt-get install -y libre2-dev pybind11-dev build-essential cmake ninja-build
fi
