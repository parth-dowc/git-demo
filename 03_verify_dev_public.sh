#!/usr/bin/env bash
set -euo pipefail
: "${NESNA_DEV_DSN:?Set DEV DSN}"
: "${NESNA_PROD_DSN:?Set read-only PROD DSN}"
mkdir -p results
python3 nesna_dev_pilot.py verify --tables permcontract --deep-verify full \
  --output results/03_first_table_verify.json
