# NESNA five-table pilot — DEV raw → existing DEV public

**Status:** scripts authored and syntax/unit checked offline; **no DEV or PROD database accessed, no rows changed.** Do not treat these files as live-verified without running the read-only comparison.

## What this does

| SQL Server source | DEV existing raw source (resolved live) | Existing DEV public destination | Legacy projection |
|---|---|---|---|
| `perm.Contract` | `raw."permContract"` *expected candidate* | `public.permcontract` | 6 columns |
| `perm.ContractField` | `raw."permContractField"` *expected candidate* | `public.permcontractfield` | 3 columns, **not** the different `contract.ContractField` |
| `perm.ContractDisbursement` | `raw."permContractDisbursement"` *expected candidate* | `public.permcontractdisbursement` | 8 columns |
| `perm.Ledger` | `raw."permLedger"` *expected candidate* | `public.permledger` | 2 columns |
| `perm.Note` | `raw."permNote"` *expected candidate* | `public.permnote` | 11 columns including `CAST(RemovedFromSource AS int)` |

Raw identifiers in the table are *illustrations* of the likely naming format. The script queries the DEV catalogs and matches the actual physical names **case-insensitively and unambiguously**, including alternative `schema_table` and `schema__table` conventions. Ambiguity or missing names aborts. **`raw_nesna` (PROD) is never used.**

These five target names correspond to exact live `staging.masterstaging.pg` values. Exact columns and order were transcribed into `five_tables.json` from the pasted masterstaging list. The runtime refuses a mismatch between that specification, live `staging.masterstaging.fields`, DEV public metadata, and available raw columns.

### 0. Prerequisites

- Execute from a machine that can access the internal DEV warehouse and has Python 3.10+, `psycopg2-binary>=2.9`, and appropriate database permissions.
- Have separate, properly configured DEV and **read-only PROD** DSNs; use `.pg_service.conf`, environment variables or a secrets manager (never commit credentials).
- DEV writer needs `SELECT` on `raw` and `staging.masterstaging`, `TRUNCATE` and `INSERT` on existing `public` tables, and permission to `CREATE` a private backup schema/table (or have a DBA provision the backup schema). Temporary-table creation permission is required.
- Ensure available DEV storage is enough for a temporary full staging copy **and** a durable backup per table. The notes/ledger tables have millions of rows.
- Coordinate the DEV India ETL pause/maintenance window. **Do not run concurrently with legacy DEV public writers.**
- Run read-only discovery SQL in `01_read_only_discovery.sql` and identify the exact current DB name and server IP from its first query.

Set environment variables for the *execution host*:

```bash
python -m pip install 'psycopg2-binary>=2.9'
export NESNA_DEV_DSN='service=nesna_warehouse_dev'  # substitute your real DEV service
export NESNA_PROD_DSN='service=nesna_warehouse_prod_readonly'  # PROD SELECT-only role
```

### 1. Compare before changing data — read only

```bash
python nesna_dev_pilot.py compare \
  --output results/01_before_comparison.json
```

Outputs JSON and CSV reports. For each table: DEV raw and public counts, PROD public counts, percentage difference, raw/public column counts, unused extra raw columns, projected field count and **exact DEV/PROD public names/types/nullability/order** comparison. Current live mapping/preflight errors will block the command. This is **not** a source-synchronized row comparison; raw was last loaded about a week earlier. PROD is opened with PostgreSQL `default_transaction_read_only` session semantics (`set_session(readonly=True)`) and only `SELECT` queries.

Review the report. The `dev_vs_prod_schema_exact` value **must be `true` for all intended tables**. DEV RAW rows and PROD PUBLIC rows can differ because of freshness; investigate material discrepancies rather than accepting an arbitrary threshold. DEV RAW extras are expected.

### 2. Load one table first — explicit DEV-only writes

There is **no automatic write or "yes" prompt**. Set explicit target DB identity using values obtained in step 0. A comparison JSON generated within 24 hours is mandatory. Coordinate legacy DEV writers before starting.

```bash
export NESNA_DEV_WRITES_ENABLED=YES
python nesna_dev_pilot.py load \
  --tables permcontract \
  --approved-preflight results/01_before_comparison.json \
  --expect-dev-db '<ACTUAL_DEV_DATABASE_NAME>' \
  --expect-dev-server-ip '<ACTUAL_DEV_SERVER_IP>' \
  --apply --confirm TRUNCATE_DEV_PUBLIC_ONLY \
  --deep-verify full \
  --output results/02_contract_load.json
```

The loader performs, **for each approved table within one DB transaction**:

1. Re-read the live mapping and schema; validate the DEV DB/server and block hazards such as inbound foreign keys, rewrite rules, triggers, RLS, or generated columns.
2. `CREATE TEMP TABLE ... LIKE public.<table> INCLUDING CONSTRAINTS ON COMMIT DROP`; insert an explicit projection from `raw` (for `permnote`, PostgreSQL `RemovedFromSource::integer`).
3. Count raw and stage, require equality, and refuse to replace populated public with an empty raw projection.
4. Acquire a table lock and `CREATE TABLE nesna_dev_migration_backup.b_<runid>_<table> AS SELECT ... FROM public.<table>`; check the backup row count. **These persistent DEV backups may be large.**
5. `TRUNCATE TABLE public.<table>` (never CASCADE), followed by an explicit-column `INSERT` from stage.
6. Validate exact stage/public row counts, full *multiset* equality via `EXCEPT ALL` in both directions (unless `--deep-verify counts` selected), and public schema/object/constraints/indexes/ACL fingerprint before **COMMIT**.
7. On any error before COMMIT, roll back the entire table transaction, including the truncation. On successful COMMIT, preserve the original data backup in DEV for recovery.

**The full equality check can consume significant DB CPU, memory, and temp disk on `permnote` / `permledger`.** Run the first table during a low-traffic window; use `--deep-verify counts` only as an explicit performance/assurance tradeoff and do additional post-load checks as needed.

### 3. Verify the first table — read only

```bash
python nesna_dev_pilot.py verify --tables permcontract \
  --deep-verify full \
  --output results/03_contract_postcheck.json
```

Verifies DEV public equals the currently loaded DEV raw projection as a complete multiset (not just row count), and compares the existing DEV public schema and approximate row count to PROD public. The `raw` DAG should **not** refresh during verification, or the snapshot comparison may legitimately diverge.

### 4. Expand to the next four tables

After the first success and review:

```bash
python nesna_dev_pilot.py load \
  --tables permcontractfield permcontractdisbursement permledger permnote \
  --approved-preflight results/01_before_comparison.json \
  --expect-dev-db '<ACTUAL_DEV_DATABASE_NAME>' \
  --expect-dev-server-ip '<ACTUAL_DEV_SERVER_IP>' \
  --apply --confirm TRUNCATE_DEV_PUBLIC_ONLY \
  --deep-verify full \
  --output results/04_remaining_load.json

python nesna_dev_pilot.py verify \
  --deep-verify full \
  --output results/05_all_postcheck.json
```

Each table is an independent transaction. A failure **stops further processing**; earlier successful tables remain committed and can be individually restored. Keep `results/*.json` and the backup-table names as a rollback record.

### 5. Restore a specific committed table from its DEV backup

The load report contains a backup name of the form `nesna_dev_migration_backup.b_YYYYMMDDTHHMMSSxxxxx_permcontract`. Extract the `YYYYMMDDTHHMMSSxxxxx` run ID, then:

```bash
python nesna_dev_pilot.py restore \
  --tables permcontract --backup-run-id '<RUN_ID_FROM_REPORT>' \
  --expect-dev-db '<ACTUAL_DEV_DATABASE_NAME>' \
  --expect-dev-server-ip '<ACTUAL_DEV_SERVER_IP>' \
  --apply --confirm RESTORE_DEV_PUBLIC_ONLY \
  --deep-verify full
```

Restore is DEV-only and transactional. Keep the backup tables for the agreed retention period and protect their permissions: `Note` may contain sensitive business/customer data. **Do not drop backups until approved.**

### Boundaries and important caveats

- This is a **pilot**, not a production scheduler and not a replacement for the already-working SQL Server → raw DAG. It never runs the raw ingestion DAG.
- The pilot strictly handles five explicitly enumerated legacy field lists. It is **not** a general T-SQL parser for the other 61 tables. In particular, other fields have `REPLACE`, `UPPER`, and SQL Server-specific casts that require separately reviewed translation.
- Existing DEV `public` table OIDs, column order/types/nullability, grants, constraints and indexes are preserved because the script does not drop/recreate them. Only data changes.
- Exact staged/published multiset equality verifies this new transfer, but **not** whether week-old DEV raw matches live SQL Server or PROD's newer India ingestion. Validation across sources requires synchronized snapshots or a mutually agreed cutoff.
- The legacy Python script uses `NOLOCK`, pandas transformations and null/whitespace cleanup in some branches; equality with India’s values is **not guaranteed just because column names and counts match**. Consider targeted value/null/whitespace checks for downstream business logic.
- Materialized views can become stale even when underlying public tables are correct. Identify and refresh **only approved DEV materialized views**, then smoke-test downstream view queries and Power BI refreshes; these scripts do not automatically refresh them.
- Schema equality and a plausible count difference are the first gate; for important financial/business columns, validate selected distributions/sums and representative identifiers before declaring the overall replacement complete.
- `TRUNCATE` needs `ACCESS EXCLUSIVE` and temporarily blocks readers while the new public dataset is inserted and checked. With `--deep-verify full` the lock remains held during a potentially expensive comparison; schedule a maintenance window.
- If the preflight finds blockers or unexpected identity/columns, **stop** and investigate rather than bypassing checks.
