#!/usr/bin/env bash
set -e
python -m pip install --upgrade pip
pip install -r requirements.txt
# لا ننشئ /var/data في الـBuild أبداً
