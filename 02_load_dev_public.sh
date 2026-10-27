#!/usr/bin/env bash
set -euo pipefail
# Explicit operator-selected DEV server/database. DO NOT hardcode credentials here.
: "${NESNA_DEV_DSN:?Set DEV-only DSN}"
: "${EXPECTED_DEV_DB:?Set actual DEV database name from read-only discovery}"
: "${EXPECTED_DEV_SERVER_IP:?Set actual DEV DB inet_server_addr() from read-only discovery}"
: "${NESNA_DEV_WRITES_ENABLED:?Explicitly export YES for intentional DEV writes}"
if [[ "$NESNA_DEV_WRITES_ENABLED" != YES ]]; then echo 'DEV writes not enabled' >&2; exit 2; fi
mkdir -p results
# One table first. Change the --tables argument ONLY after reviewing first-table validation.
python3 nesna_dev_pilot.py load \
  --tables permcontract \
  --approved-preflight results/01_before_comparison.json \
  --expect-dev-db "$EXPECTED_DEV_DB" \
  --expect-dev-server-ip "$EXPECTED_DEV_SERVER_IP" \
  --apply --confirm TRUNCATE_DEV_PUBLIC_ONLY \
  --deep-verify full \
  --output results/02_first_table_load.json
