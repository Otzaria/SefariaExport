#!/usr/bin/env bash
set -euo pipefail

# SEFARIA_PROJECT_REF pins the checkout to an exact commit/tag. Left empty the
# clone tracks the default branch, which is fine for text content but NOT for
# the link-visibility masks: those are computed by this checkout's own
# get_links() helpers, so the commit behind them has to be recorded.
REF="${SEFARIA_PROJECT_REF:-}"

if [ -d "Sefaria-Project" ] && [ -n "$REF" ]; then
  echo "Updating existing Sefaria-Project checkout to pinned ref $REF"
  git -C Sefaria-Project fetch -q --depth 1 origin "$REF"
  git -C Sefaria-Project checkout -q --detach FETCH_HEAD
elif [ -d "Sefaria-Project" ]; then
  echo "Sefaria-Project already exists, using its current checkout"
elif [ -n "$REF" ]; then
  echo "Cloning Sefaria-Project pinned at $REF"
  git init -q Sefaria-Project
  git -C Sefaria-Project remote add origin https://github.com/Sefaria/Sefaria-Project.git
  git -C Sefaria-Project fetch -q --depth 1 origin "$REF"
  git -C Sefaria-Project checkout -q FETCH_HEAD
else
  git clone --depth 1 https://github.com/Sefaria/Sefaria-Project.git
fi

# Recorded unconditionally: `release_metadata.source_commit` describes the
# SefariaExport commit, not the Sefaria-Project one, so without this the
# visibility policy behind an export cannot be reproduced.
git -C Sefaria-Project rev-parse HEAD > Sefaria-Project.sha
echo "Sefaria-Project at $(cat Sefaria-Project.sha)"
ls -la Sefaria-Project | head -n 50
