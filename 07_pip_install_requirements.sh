#!/usr/bin/env bash
set -euo pipefail

cd Sefaria-Project
echo "google-re2==1.1.20240702" > constraints-ci.txt
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt -c constraints-ci.txt
