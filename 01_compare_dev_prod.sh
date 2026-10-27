#!/usr/bin/env bash
set -euo pipefail
: "${NESNA_DEV_DSN:?Set DEV DSN via secret manager or libpq service}"
: "${NESNA_PROD_DSN:?Set read-only PROD DSN via secret manager or libpq service}"
mkdir -p results
python3 nesna_dev_pilot.py compare --output results/01_before_comparison.json
