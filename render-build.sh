#!/usr/bin/env bash
set -e
python -m pip install --upgrade pip
pip install -r requirements.txt
# Don't create /var/data at build time; Render build FS is read-only.
