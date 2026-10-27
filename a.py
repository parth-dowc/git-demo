#New-Item -ItemType Directory -Force -Path results | Out-Null

#python .\nesna_dev_pilot.py compare --output results/01_before_comparison.json

#!/usr/bin/env python3
"""
NESNA FIVE-TABLE DEV MIGRATION

DEV source:       raw
DEV destination:  nesna
PROD comparison:  nesna (READ ONLY)
DEV backups:      nesna_dev_migration_backup

Commands:
    compare  - Read-only comparison of DEV raw, DEV nesna, PROD nesna
    load     - Backup, stage, truncate, load and validate DEV nesna
    verify   - Read-only verification after loading
    restore  - Restore DEV nesna from a saved backup

Requirements:
    pip install psycopg2-binary

Environment:
    NESNA_DEV_DSN
    NESNA_PROD_DSN
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import uuid

import psycopg2
from psycopg2 import sql


# ============================================================
# CONFIGURATION
# ============================================================

RAW_SCHEMA = "raw"
TARGET_SCHEMA = "nesna"
PROD_SCHEMA = "nesna"

MASTERSTAGING_SCHEMA = "staging"
BACKUP_SCHEMA = "nesna_dev_migration_backup"

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "five_tables.json"

FIELD_SIMPLE = re.compile(
    r"^(?:\[([A-Za-z][A-Za-z_0-9]*)\]|([A-Za-z][A-Za-z_0-9]*))$"
)

FIELD_CAST_INT = re.compile(
    r"^CAST\s*\(\s*"
    r"(?:\[([A-Za-z][A-Za-z_0-9]*)\]|([A-Za-z][A-Za-z_0-9]*))"
    r"\s+AS\s+INT\s*\)\s+"
    r"(?:AS\s+)?([A-Za-z][A-Za-z_0-9]*)$",
    re.I,
)


# ============================================================
# CONFIGURATION LOADING
# ============================================================

def config():
    specs = json.loads(CONFIG.read_text(encoding="utf-8"))["tables"]

    if len(specs) != 5:
        raise RuntimeError("Configuration must contain exactly five tables")

    names = [x["public"] for x in specs]

    if len(set(names)) != 5:
        raise RuntimeError("Duplicate destination tables")

    return {x["public"]: x for x in specs}


def source_key(source):
    parts = source.replace("[", "").replace("]", "").split(".")

    if len(parts) != 2:
        raise ValueError(f"Invalid source identifier: {source}")

    for part in parts:
        if not re.fullmatch(r"[A-Za-z][A-Za-z_0-9]*", part):
            raise ValueError(f"Unsafe source identifier: {source}")

    return tuple(x.casefold() for x in parts)


def find_unique(name, names, context):
    matches = [
        value for value in names
        if value.casefold() == name.casefold()
    ]

    if len(matches) != 1:
        raise ValueError(
            f"{context}: expected one match for {name}, found {matches}"
        )

    return matches[0]


def raw_name_for(source, available):
    schema, table = source_key(source)

    candidates = {
        schema + table,
        schema + "_" + table,
        schema + "__" + table,
        schema + "." + table,
    }

    matches = [
        name for name in available
        if name.casefold() in candidates
    ]

    if len(matches) != 1:
        raise ValueError(
            f"Cannot uniquely resolve DEV raw source {source}: {matches}"
        )

    return matches[0]


# ============================================================
# FIELD PARSING
# ============================================================

def split_tsql_fields(value):
    result = []
    buffer = []

    depth = 0
    bracket = False
    quoted = False
    i = 0

    while i < len(value):
        ch = value[i]
        nxt = value[i + 1] if i + 1 < len(value) else ""

        if quoted:
            buffer.append(ch)

            if ch == "'":
                if nxt == "'":
                    buffer.append(nxt)
                    i += 1
                else:
                    quoted = False

        elif bracket:
            buffer.append(ch)

            if ch == "]":
                if nxt == "]":
                    buffer.append(nxt)
                    i += 1
                else:
                    bracket = False

        elif ch == "'":
            quoted = True
            buffer.append(ch)

        elif ch == "[":
            bracket = True
            buffer.append(ch)

        elif ch == "(":
            depth += 1
            buffer.append(ch)

        elif ch == ")":
            depth -= 1

            if depth < 0:
                raise ValueError("Unbalanced field expression")

            buffer.append(ch)

        elif ch == "," and depth == 0:
            result.append("".join(buffer).strip())
            buffer = []

        else:
            buffer.append(ch)

        i += 1

    if depth or bracket or quoted:
        raise ValueError("Unbalanced masterstaging fields")

    result.append("".join(buffer).strip())

    if not all(result):
        raise ValueError("Empty field expression")

    return result


def parse_expression(expression):
    expression = expression.strip()

    match = FIELD_SIMPLE.fullmatch(expression)

    if match:
        source = match.group(1) or match.group(2)
        return "column", source, source

    match = FIELD_CAST_INT.fullmatch(expression)

    if match:
        source = match.group(1) or match.group(2)
        return "int", source, match.group(3)

    raise ValueError(
        f"Unsupported masterstaging expression: {expression}"
    )


# ============================================================
# DATABASE HELPERS
# ============================================================

def connect(dsn, readonly=True):
    if not dsn:
        raise RuntimeError(
            "Database connection missing. Configure "
            "NESNA_DEV_DSN and NESNA_PROD_DSN."
        )

    conn = psycopg2.connect(
        dsn,
        connect_timeout=15,
        application_name="nesna_dev_pilot",
    )

    conn.set_session(
        readonly=readonly,
        isolation_level=(
            "REPEATABLE READ" if readonly else "READ COMMITTED"
        ),
        autocommit=False,
    )

    return conn


def qtable(schema, table):
    return sql.SQL("{}.{}").format(
        sql.Identifier(schema),
        sql.Identifier(table),
    )


def identity(cur):
    cur.execute(
        """
        SELECT
            current_database(),
            inet_server_addr()::text,
            inet_server_port(),
            current_user
        """
    )

    return cur.fetchone()


def tables_in(cur, schema):
    cur.execute(
        """
        SELECT c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n
            ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind = 'r'
          AND NOT c.relispartition
        """,
        (schema,),
    )

    return [row[0] for row in cur.fetchall()]


def cols(cur, schema, table):
    cur.execute(
        """
        SELECT
            a.attname,
            format_type(a.atttypid, a.atttypmod),
            a.attnotnull,
            a.attgenerated,
            a.attidentity,
            pg_get_expr(d.adbin, d.adrelid),
            a.attcollation::oid
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c
            ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n
            ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef d
            ON d.adrelid = a.attrelid
           AND d.adnum = a.attnum
        WHERE n.nspname = %s
          AND c.relname = %s
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (schema, table),
    )

    return [
        {
            "name": row[0],
            "type": row[1],
            "not_null": row[2],
            "generated": row[3],
            "identity": row[4],
            "default": row[5],
            "collation": row[6],
        }
        for row in cur.fetchall()
    ]


def count(cur, schema, table):
    cur.execute(
        sql.SQL("SELECT COUNT(*) FROM {}").format(
            qtable(schema, table)
        )
    )

    return cur.fetchone()[0]


def column_signature(columns):
    return [
        [c["name"], c["type"], c["not_null"]]
        for c in columns
    ]


# ============================================================
# SCHEMA SAFETY
# ============================================================

def structural_signature(cur, table):
    cur.execute(
        """
        SELECT
            c.oid::text,
            c.relkind,
            c.relispartition,
            c.relrowsecurity,
            c.relforcerowsecurity,
            pg_get_userbyid(c.relowner),
            c.relacl::text,

            COALESCE(
                (
                    SELECT jsonb_agg(
                        pg_get_constraintdef(con.oid)
                        ORDER BY con.conname
                    )::text
                    FROM pg_constraint con
                    WHERE con.conrelid = c.oid
                ),
                '[]'
            ),

            COALESCE(
                (
                    SELECT jsonb_agg(
                        pg_get_indexdef(idx.indexrelid)
                        ORDER BY idx.indexrelid
                    )::text
                    FROM pg_index idx
                    WHERE idx.indrelid = c.oid
                ),
                '[]'
            ),

            COALESCE(
                (
                    SELECT jsonb_agg(
                        pg_get_triggerdef(tr.oid)
                        ORDER BY tr.tgname
                    )::text
                    FROM pg_trigger tr
                    WHERE tr.tgrelid = c.oid
                      AND NOT tr.tgisinternal
                ),
                '[]'
            )

        FROM pg_class c
        JOIN pg_namespace n
            ON n.oid = c.relnamespace

        WHERE n.nspname = %s
          AND c.relname = %s
        """,
        (TARGET_SCHEMA, table),
    )

    return (
        cur.fetchone(),
        cols(cur, TARGET_SCHEMA, table),
    )


def target_hazards(cur, table):
    cur.execute(
        """
        SELECT
            c.oid,
            c.relkind,
            c.relispartition,
            c.relrowsecurity,
            c.relforcerowsecurity
        FROM pg_class c
        JOIN pg_namespace n
            ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relname = %s
        """,
        (TARGET_SCHEMA, table),
    )

    record = cur.fetchone()

    if not record:
        raise RuntimeError(
            f"DEV target missing: {TARGET_SCHEMA}.{table}"
        )

    oid, kind, partitioned, rls, forced_rls = record

    if kind != "r" or partitioned or rls or forced_rls:
        raise RuntimeError(
            f"Unsafe DEV target configuration: {TARGET_SCHEMA}.{table}"
        )

    cur.execute(
        """
        SELECT conname
        FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid = %s
        """,
        (oid,),
    )

    foreign_keys = [x[0] for x in cur.fetchall()]

    if foreign_keys:
        raise RuntimeError(
            f"Inbound foreign keys block independent TRUNCATE: "
            f"{foreign_keys}"
        )

    cur.execute(
        """
        SELECT tgname
        FROM pg_trigger
        WHERE tgrelid = %s
          AND NOT tgisinternal
        """,
        (oid,),
    )

    triggers = [x[0] for x in cur.fetchall()]

    if triggers:
        raise RuntimeError(
            f"Target has triggers requiring review: {triggers}"
        )

    cur.execute(
        """
        SELECT rulename
        FROM pg_rules
        WHERE schemaname = %s
          AND tablename = %s
        """,
        (TARGET_SCHEMA, table),
    )

    if cur.fetchall():
        raise RuntimeError(
            f"Target has rewrite rules: {TARGET_SCHEMA}.{table}"
        )


# ============================================================
# MASTERSTAGING VALIDATION
# ============================================================

def live_masterstaging(cur, spec):
    cur.execute(
        sql.SQL("SELECT ms, pg, fields FROM {}.{}").format(
            sql.Identifier(MASTERSTAGING_SCHEMA),
            sql.Identifier("masterstaging"),
        )
    )

    matches = []

    for ms, pg, fields in cur.fetchall():
        if (
            source_key(ms) == source_key(spec["source"])
            and str(pg).casefold() == spec["public"].casefold()
        ):
            matches.append((ms, pg, fields))

    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one masterstaging mapping for "
            f"{spec['source']} -> {spec['public']}; "
            f"found {len(matches)}"
        )

    live_fields = [
        parse_expression(x)
        for x in split_tsql_fields(matches[0][2])
    ]

    configured_fields = [
        parse_expression(x)
        for x in spec["fields"]
    ]

    normalize = lambda values: [
        (kind, source.casefold(), output.casefold())
        for kind, source, output in values
    ]

    if normalize(live_fields) != normalize(configured_fields):
        raise RuntimeError(
            f"Masterstaging fields changed for {spec['public']}"
        )


def build_projection(spec, raw_columns, target_columns):
    parsed = [
        parse_expression(field)
        for field in spec["fields"]
    ]

    actual_targets = [
        c["name"] for c in target_columns
    ]

    outputs = [
        output for _, _, output in parsed
    ]

    if (
        [x.casefold() for x in outputs]
        != [x.casefold() for x in actual_targets]
    ):
        raise RuntimeError(
            f"Target columns differ from projection.\n"
            f"Expected: {outputs}\n"
            f"Actual: {actual_targets}"
        )

    if len(set(x.casefold() for x in outputs)) != len(outputs):
        raise RuntimeError("Duplicate projected columns")

    if any(
        c["identity"] or c["generated"]
        for c in target_columns
    ):
        raise RuntimeError(
            "Generated or identity target columns require review"
        )

    raw_names = [
        c["name"] for c in raw_columns
    ]

    expressions = []

    for kind, source, output in parsed:
        actual_source = find_unique(
            source,
            raw_names,
            "DEV raw column",
        )

        actual_output = find_unique(
            output,
            actual_targets,
            "DEV nesna column",
        )

        expression = sql.Identifier(actual_source)

        if kind == "int":
            expression = sql.SQL("({})::integer").format(
                expression
            )

        expressions.append(
            sql.SQL("{} AS {}").format(
                expression,
                sql.Identifier(actual_output),
            )
        )

    return (
        sql.SQL(", ").join(expressions),
        actual_targets,
    )


def discover(cur, spec):
    live_masterstaging(cur, spec)

    raw_name = raw_name_for(
        spec["source"],
        tables_in(cur, RAW_SCHEMA),
    )

    target_name = find_unique(
        spec["public"],
        tables_in(cur, TARGET_SCHEMA),
        "DEV nesna table",
    )

    raw_columns = cols(
        cur,
        RAW_SCHEMA,
        raw_name,
    )

    target_columns = cols(
        cur,
        TARGET_SCHEMA,
        target_name,
    )

    target_hazards(cur, target_name)

    projection, names = build_projection(
        spec,
        raw_columns,
        target_columns,
    )

    if spec.get("key"):
        find_unique(
            spec["key"],
            names,
            "Pilot key",
        )

    return {
        "spec": spec,
        "raw_name": raw_name,
        "target_name": target_name,
        "raw_columns": raw_columns,
        "target_columns": target_columns,
        "projection": projection,
        "names": names,
    }


# ============================================================
# READ-ONLY COMPARISON
# ============================================================

def report_row(dev_cur, obj, prod_cur=None):
    raw_name = obj["raw_name"]
    target_name = obj["target_name"]

    raw_count = count(
        dev_cur,
        RAW_SCHEMA,
        raw_name,
    )

    dev_target_count = count(
        dev_cur,
        TARGET_SCHEMA,
        target_name,
    )

    prod_count = None
    prod_columns = None
    schema_match = None

    if prod_cur is not None:
        prod_name = find_unique(
            target_name,
            tables_in(prod_cur, PROD_SCHEMA),
            "PROD nesna table",
        )

        prod_columns = cols(
            prod_cur,
            PROD_SCHEMA,
            prod_name,
        )

        prod_count = count(
            prod_cur,
            PROD_SCHEMA,
            prod_name,
        )

        schema_match = (
            column_signature(prod_columns)
            == column_signature(obj["target_columns"])
        )

    required_sources = {
        parse_expression(field)[1].casefold()
        for field in obj["spec"]["fields"]
    }

    extra_columns = [
        c["name"]
        for c in obj["raw_columns"]
        if c["name"].casefold() not in required_sources
    ]

    difference = None

    if prod_count:
        difference = round(
            100.0 * (raw_count - prod_count) / prod_count,
            3,
        )

    return {
        "generated_at_utc": dt.datetime.now(
            dt.timezone.utc
        ).isoformat(),

        "source": obj["spec"]["source"],
        "raw": f"{RAW_SCHEMA}.{raw_name}",
        "target": f"{TARGET_SCHEMA}.{target_name}",

        "dev_target_schema": TARGET_SCHEMA,
        "prod_schema": (
            PROD_SCHEMA if prod_cur is not None else None
        ),

        "raw_columns": len(obj["raw_columns"]),
        "source_fields": len(obj["spec"]["fields"]),

        "dev_target_columns": len(obj["target_columns"]),
        "prod_target_columns": (
            len(prod_columns)
            if prod_columns is not None
            else None
        ),

        "dev_target_schema_signature": column_signature(
            obj["target_columns"]
        ),

        "prod_target_schema_signature": (
            column_signature(prod_columns)
            if prod_columns is not None
            else None
        ),

        "dev_vs_prod_schema_exact": schema_match,

        "unused_raw_columns": extra_columns,

        "dev_raw_rows": raw_count,
        "dev_target_before_rows": dev_target_count,
        "prod_target_rows": prod_count,

        "dev_raw_vs_prod_pct": difference,
    }


def summary_files(records, filename):
    path = Path(filename)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(records, indent=2, default=str),
        encoding="utf-8",
    )

    if isinstance(records, list):
        csv_path = path.with_suffix(".csv")

        fields = [
            "source",
            "raw",
            "target",
            "raw_columns",
            "source_fields",
            "dev_target_columns",
            "prod_target_columns",
            "dev_vs_prod_schema_exact",
            "dev_raw_rows",
            "dev_target_before_rows",
            "prod_target_rows",
            "dev_raw_vs_prod_pct",
        ]

        with csv_path.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=fields,
                extrasaction="ignore",
            )

            writer.writeheader()
            writer.writerows(records)

        print("CSV:", csv_path)

    print("JSON:", path)


def cmd_compare(args):
    specs = config()

    dev = connect(
        os.environ.get("NESNA_DEV_DSN"),
        readonly=True,
    )

    prod = (
        connect(
            os.environ.get("NESNA_PROD_DSN"),
            readonly=True,
        )
        if not args.no_prod
        else None
    )

    records = []

    try:
        with dev.cursor() as dc:
            dev_identity = identity(dc)

            print("DEV:", dev_identity)

            if prod:
                with prod.cursor() as pc:
                    prod_identity = identity(pc)

                    print("PROD:", prod_identity)

                    if dev_identity[:3] == prod_identity[:3]:
                        raise RuntimeError(
                            "DEV and PROD point to the same database"
                        )

                    for table in args.tables:
                        obj = discover(dc, specs[table])

                        row = report_row(dc, obj, pc)

                        row["dev_identity"] = list(
                            dev_identity[:3]
                        )

                        row["prod_identity"] = list(
                            prod_identity[:3]
                        )

                        records.append(row)

                        print(
                            f"{table}: "
                            f"raw={row['dev_raw_rows']:,}, "
                            f"DEV nesna={row['dev_target_before_rows']:,}, "
                            f"PROD nesna={row['prod_target_rows']:,}, "
                            f"schema_match={row['dev_vs_prod_schema_exact']}"
                        )

            else:
                for table in args.tables:
                    obj = discover(dc, specs[table])

                    row = report_row(dc, obj)

                    records.append(row)

                    print(
                        f"{table}: "
                        f"raw={row['dev_raw_rows']:,}, "
                        f"DEV nesna={row['dev_target_before_rows']:,}"
                    )

        summary_files(records, args.output)

    finally:
        dev.close()

        if prod:
            prod.close()


# ============================================================
# WRITE SAFETY
# ============================================================

def assert_dev_identity(cur, args):
    database, host, port, role = identity(cur)

    if not args.expect_dev_db or not args.expect_dev_server_ip:
        raise RuntimeError(
            "DEV writes require --expect-dev-db "
            "and --expect-dev-server-ip"
        )

    if (
        database != args.expect_dev_db
        or host != args.expect_dev_server_ip
    ):
        raise RuntimeError(
            f"WRITE BLOCKED: connected to {(database, host)}, "
            f"expected {(args.expect_dev_db, args.expect_dev_server_ip)}"
        )

    if (
        database.casefold().endswith("_prod")
        or "production" in database.casefold()
    ):
        raise RuntimeError(
            "WRITE BLOCKED: production-labelled database"
        )

    return {
        "database": database,
        "server_ip": host,
        "port": port,
        "role": role,
    }


def deep_exact_equal(cur, left, right, names):
    projection = sql.SQL(", ").join(
        sql.Identifier(name)
        for name in names
    )

    for a, b in [
        (left, right),
        (right, left),
    ]:
        query = sql.SQL(
            """
            SELECT EXISTS (
                SELECT 1
                FROM (
                    SELECT {} FROM {}
                    EXCEPT ALL
                    SELECT {} FROM {}
                ) AS delta
                LIMIT 1
            )
            """
        ).format(
            projection,
            a,
            projection,
            b,
        )

        cur.execute(query)

        if cur.fetchone()[0]:
            raise RuntimeError(
                "Exact row comparison failed"
            )


# ============================================================
# DEV LOAD
# ============================================================

def perform_load(conn, obj, args, run_id):
    target = qtable(
        TARGET_SCHEMA,
        obj["target_name"],
    )

    source = qtable(
        RAW_SCHEMA,
        obj["raw_name"],
    )

    stage = sql.Identifier("nesna_dev_stage")

    columns = sql.SQL(", ").join(
        sql.Identifier(name)
        for name in obj["names"]
    )

    backup_name = (
        f"b_{run_id}_{obj['target_name']}"
    )

    if len(backup_name.encode()) > 63:
        raise RuntimeError(
            "Backup table name exceeds PostgreSQL limit"
        )

    backup = qtable(
        BACKUP_SCHEMA,
        backup_name,
    )

    with conn.cursor() as cur:
        assert_dev_identity(cur, args)

        cur.execute(
            "SET LOCAL lock_timeout = %s",
            ("15s",),
        )

        cur.execute(
            "SET LOCAL statement_timeout = %s",
            ("12h",),
        )

        cur.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtext(%s),
                hashtext(%s)
            )
            """,
            (
                "nesna_raw_to_nesna_dev",
                obj["target_name"],
            ),
        )

        # Lock the raw source so it cannot change during
        # staging and publication.
        cur.execute(
            sql.SQL(
                "LOCK TABLE {} IN SHARE MODE"
            ).format(source)
        )

        live = discover(
            cur,
            obj["spec"],
        )

        if (
            live["raw_name"] != obj["raw_name"]
            or live["target_name"] != obj["target_name"]
        ):
            raise RuntimeError(
                "Mapping changed after preflight"
            )

        old_signature = structural_signature(
            cur,
            obj["target_name"],
        )

        # Stage selected columns.
        cur.execute(
            sql.SQL(
                """
                CREATE TEMP TABLE {}
                (LIKE {} INCLUDING CONSTRAINTS)
                ON COMMIT DROP
                """
            ).format(stage, target)
        )

        cur.execute(
            sql.SQL(
                """
                INSERT INTO {} ({})
                SELECT {}
                FROM {}
                """
            ).format(
                stage,
                columns,
                obj["projection"],
                source,
            )
        )

        inserted = cur.rowcount

        cur.execute(
            sql.SQL(
                "SELECT COUNT(*) FROM {}"
            ).format(stage)
        )

        staged = cur.fetchone()[0]

        raw_count = count(
            cur,
            RAW_SCHEMA,
            obj["raw_name"],
        )

        if not (
            inserted == staged == raw_count
        ):
            raise RuntimeError(
                f"Staging mismatch: "
                f"raw={raw_count}, "
                f"inserted={inserted}, "
                f"staged={staged}"
            )

        # Lock destination before backup.
        cur.execute(
            sql.SQL(
                "LOCK TABLE {} IN SHARE MODE"
            ).format(target)
        )

        previous_count = count(
            cur,
            TARGET_SCHEMA,
            obj["target_name"],
        )

        if staged == 0 and previous_count > 0:
            raise RuntimeError(
                "Refusing to replace populated target "
                "with zero rows"
            )

        # Create persistent DEV backup.
        cur.execute(
            sql.SQL(
                "CREATE SCHEMA IF NOT EXISTS {}"
            ).format(
                sql.Identifier(BACKUP_SCHEMA)
            )
        )

        cur.execute(
            "SELECT to_regclass(%s)",
            (f"{BACKUP_SCHEMA}.{backup_name}",),
        )

        if cur.fetchone()[0] is not None:
            raise RuntimeError(
                "Backup already exists"
            )

        cur.execute(
            sql.SQL(
                """
                CREATE TABLE {} AS
                SELECT {}
                FROM {}
                """
            ).format(
                backup,
                columns,
                target,
            )
        )

        backup_count = count(
            cur,
            BACKUP_SCHEMA,
            backup_name,
        )

        if backup_count != previous_count:
            raise RuntimeError(
                "Backup row count mismatch"
            )

        if structural_signature(
            cur,
            obj["target_name"],
        ) != old_signature:
            raise RuntimeError(
                "Target structure changed"
            )

        # Transactional replacement.
        # No CASCADE.
        cur.execute(
            sql.SQL(
                "TRUNCATE TABLE {}"
            ).format(target)
        )

        cur.execute(
            sql.SQL(
                """
                INSERT INTO {} ({})
                SELECT {}
                FROM {}
                """
            ).format(
                target,
                columns,
                columns,
                stage,
            )
        )

        published = cur.rowcount

        final_count = count(
            cur,
            TARGET_SCHEMA,
            obj["target_name"],
        )

        if published != staged or final_count != staged:
            raise RuntimeError(
                "Published row count mismatch"
            )

        if args.deep_verify == "full":
            deep_exact_equal(
                cur,
                stage,
                target,
                obj["names"],
            )

        if structural_signature(
            cur,
            obj["target_name"],
        ) != old_signature:
            raise RuntimeError(
                "Target structure changed during publication"
            )

        return {
            "status": "SUCCESS",
            "raw": f"{RAW_SCHEMA}.{obj['raw_name']}",
            "target": f"{TARGET_SCHEMA}.{obj['target_name']}",
            "backup": f"{BACKUP_SCHEMA}.{backup_name}",
            "before_rows": previous_count,
            "stage_rows": staged,
            "published_rows": published,
            "deep_verification": args.deep_verify,
        }


def cmd_load(args):
    specs = config()

    if (
        not args.apply
        or args.confirm != "TRUNCATE_DEV_NESNA_ONLY"
    ):
        raise SystemExit(
            "WRITE BLOCKED: require "
            "--apply --confirm TRUNCATE_DEV_NESNA_ONLY"
        )

    if os.getenv("NESNA_DEV_WRITES_ENABLED") != "YES":
        raise SystemExit(
            "WRITE BLOCKED: "
            "NESNA_DEV_WRITES_ENABLED must be YES"
        )

    if not args.expect_dev_db or not args.expect_dev_server_ip:
        raise SystemExit(
            "WRITE BLOCKED: DEV identity required"
        )

    if not args.approved_preflight:
        raise SystemExit(
            "WRITE BLOCKED: approved preflight required"
        )

    approved = json.loads(
        Path(args.approved_preflight).read_text(
            encoding="utf-8"
        )
    )

    for table in args.tables:
        matches = [
            row for row in approved
            if row.get("target") == f"{TARGET_SCHEMA}.{table}"
        ]

        if len(matches) != 1:
            raise SystemExit(
                f"WRITE BLOCKED: missing preflight for {table}"
            )

        record = matches[0]

        if record.get("dev_vs_prod_schema_exact") is not True:
            raise SystemExit(
                f"WRITE BLOCKED: schema mismatch for {table}"
            )

        if record.get("prod_schema") != PROD_SCHEMA:
            raise SystemExit(
                f"WRITE BLOCKED: incorrect PROD schema for {table}"
            )

        if not record.get("prod_identity"):
            raise SystemExit(
                f"WRITE BLOCKED: PROD comparison missing for {table}"
            )

        if not record.get("dev_target_schema_signature"):
            raise SystemExit(
                f"WRITE BLOCKED: DEV schema signature missing"
            )

        dev_id = record.get("dev_identity", [])
        prod_id = record.get("prod_identity", [])

        if len(dev_id) < 3 or len(prod_id) < 3:
            raise SystemExit(
                "WRITE BLOCKED: incomplete database identities"
            )

        if dev_id[:3] == prod_id[:3]:
            raise SystemExit(
                "WRITE BLOCKED: DEV and PROD identities match"
            )

        if dev_id[:2] != [
            args.expect_dev_db,
            args.expect_dev_server_ip,
        ]:
            raise SystemExit(
                "WRITE BLOCKED: DEV identity mismatch"
            )

        timestamp = dt.datetime.fromisoformat(
            record["generated_at_utc"]
        )

        age = (
            dt.datetime.now(dt.timezone.utc)
            - timestamp
        )

        if age < dt.timedelta(0) or age > dt.timedelta(hours=24):
            raise SystemExit(
                f"WRITE BLOCKED: stale preflight for {table}"
            )

    run_id = (
        dt.datetime.now(dt.timezone.utc).strftime(
            "%Y%m%dT%H%M%S"
        )
        + uuid.uuid4().hex[:5]
    )

    results = []

    for table in args.tables:
        conn = connect(
            os.environ.get("NESNA_DEV_DSN"),
            readonly=False,
        )

        try:
            with conn.cursor() as cur:
                assert_dev_identity(cur, args)

                obj = discover(
                    cur,
                    specs[table],
                )

                saved = next(
                    row for row in approved
                    if row["target"] == f"{TARGET_SCHEMA}.{table}"
                )

                current_signature = column_signature(
                    obj["target_columns"]
                )

                if (
                    current_signature
                    != saved["dev_target_schema_signature"]
                ):
                    raise RuntimeError(
                        "DEV target schema changed since preflight"
                    )

                if (
                    f"{RAW_SCHEMA}.{obj['raw_name']}"
                    != saved["raw"]
                ):
                    raise RuntimeError(
                        "DEV raw mapping changed since preflight"
                    )

            result = perform_load(
                conn,
                obj,
                args,
                run_id,
            )

            conn.commit()

            results.append(result)

            print(
                f"COMMITTED {table}: "
                f"{result['published_rows']:,} rows"
            )

            print(
                f"BACKUP: {result['backup']}"
            )

        except Exception as exc:
            conn.rollback()

            results.append({
                "status": "FAILED",
                "target": f"{TARGET_SCHEMA}.{table}",
                "error": str(exc),
            })

            print(
                f"FAILED {table}: {exc}",
                file=sys.stderr,
            )

            break

        finally:
            conn.close()

            summary_files(
                results,
                args.output,
            )

    if (
        len(results) != len(args.tables)
        or any(
            row["status"] != "SUCCESS"
            for row in results
        )
    ):
        raise SystemExit(1)


# ============================================================
# READ-ONLY VERIFICATION
# ============================================================

def cmd_verify(args):
    specs = config()

    dev = connect(
        os.environ.get("NESNA_DEV_DSN"),
        readonly=True,
    )

    prod = (
        connect(
            os.environ.get("NESNA_PROD_DSN"),
            readonly=True,
        )
        if not args.no_prod
        else None
    )

    records = []

    try:
        with dev.cursor() as dc:
            pc = prod.cursor() if prod else None

            try:
                if pc and identity(dc)[:3] == identity(pc)[:3]:
                    raise RuntimeError(
                        "DEV and PROD resolve to the same database"
                    )

                for table in args.tables:
                    obj = discover(
                        dc,
                        specs[table],
                    )

                    row = report_row(
                        dc,
                        obj,
                        pc,
                    )

                    target = qtable(
                        TARGET_SCHEMA,
                        obj["target_name"],
                    )

                    projected = sql.SQL(
                        "(SELECT {} FROM {}) AS projected_raw"
                    ).format(
                        obj["projection"],
                        qtable(
                            RAW_SCHEMA,
                            obj["raw_name"],
                        ),
                    )

                    counts_match = (
                        row["dev_raw_rows"]
                        == row["dev_target_before_rows"]
                    )

                    row["dev_target_matches_raw_count"] = (
                        counts_match
                    )

                    if not counts_match:
                        raise RuntimeError(
                            f"{table}: raw and DEV nesna "
                            "row counts differ"
                        )

                    if row["dev_vs_prod_schema_exact"] is False:
                        raise RuntimeError(
                            f"{table}: DEV/PROD schema mismatch"
                        )

                    if args.deep_verify == "full":
                        deep_exact_equal(
                            dc,
                            projected,
                            target,
                            obj["names"],
                        )

                        row["dev_target_exact_raw_rows"] = True

                    else:
                        row["dev_target_exact_raw_rows"] = (
                            "NOT TESTED"
                        )

                    records.append(row)

                    print(
                        f"PASSED: {TARGET_SCHEMA}.{table}"
                    )

            finally:
                if pc:
                    pc.close()

        summary_files(
            records,
            args.output,
        )

    finally:
        dev.close()

        if prod:
            prod.close()


# ============================================================
# RESTORE DEV BACKUP
# ============================================================

def cmd_restore(args):
    if (
        not args.apply
        or args.confirm != "RESTORE_DEV_NESNA_ONLY"
        or os.getenv("NESNA_DEV_WRITES_ENABLED") != "YES"
    ):
        raise SystemExit(
            "Restore requires --apply, "
            "--confirm RESTORE_DEV_NESNA_ONLY, "
            "and NESNA_DEV_WRITES_ENABLED=YES"
        )

    if len(args.tables) != 1:
        raise SystemExit(
            "Restore requires exactly one table"
        )

    if not args.backup_run_id:
        raise SystemExit(
            "Restore requires --backup-run-id"
        )

    if not re.fullmatch(
        r"\d{8}T\d{6}[a-f0-9]{5}",
        args.backup_run_id,
    ):
        raise SystemExit(
            "Invalid backup run ID"
        )

    table = args.tables[0]

    backup_name = (
        f"b_{args.backup_run_id}_{table}"
    )

    conn = connect(
        os.environ.get("NESNA_DEV_DSN"),
        readonly=False,
    )

    try:
        with conn.cursor() as cur:
            assert_dev_identity(cur, args)

            target_name = find_unique(
                table,
                tables_in(cur, TARGET_SCHEMA),
                "DEV restore target",
            )

            backup_name = find_unique(
                backup_name,
                tables_in(cur, BACKUP_SCHEMA),
                "DEV backup",
            )

            target_columns = cols(
                cur,
                TARGET_SCHEMA,
                target_name,
            )

            backup_columns = cols(
                cur,
                BACKUP_SCHEMA,
                backup_name,
            )

            target_signature = [
                (c["name"], c["type"])
                for c in target_columns
            ]

            backup_signature = [
                (c["name"], c["type"])
                for c in backup_columns
            ]

            if target_signature != backup_signature:
                raise RuntimeError(
                    "Backup and target columns/types differ"
                )

            target_hazards(
                cur,
                target_name,
            )

            old_structure = structural_signature(
                cur,
                target_name,
            )

            target = qtable(
                TARGET_SCHEMA,
                target_name,
            )

            backup = qtable(
                BACKUP_SCHEMA,
                backup_name,
            )

            columns = sql.SQL(", ").join(
                sql.Identifier(c["name"])
                for c in target_columns
            )

            cur.execute(
                "SET LOCAL lock_timeout = %s",
                ("15s",),
            )

            cur.execute(
                sql.SQL(
                    "LOCK TABLE {} IN SHARE MODE"
                ).format(target)
            )

            backup_count = count(
                cur,
                BACKUP_SCHEMA,
                backup_name,
            )

            cur.execute(
                sql.SQL(
                    "TRUNCATE TABLE {}"
                ).format(target)
            )

            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {} ({})
                    SELECT {}
                    FROM {}
                    """
                ).format(
                    target,
                    columns,
                    columns,
                    backup,
                )
            )

            restored = cur.rowcount

            final_count = count(
                cur,
                TARGET_SCHEMA,
                target_name,
            )

            if (
                restored != backup_count
                or final_count != backup_count
            ):
                raise RuntimeError(
                    "Restore row count mismatch"
                )

            if args.deep_verify == "full":
                deep_exact_equal(
                    cur,
                    target,
                    backup,
                    [
                        c["name"]
                        for c in target_columns
                    ],
                )

            if structural_signature(
                cur,
                target_name,
            ) != old_structure:
                raise RuntimeError(
                    "Target structure changed during restore"
                )

        conn.commit()

        print(
            f"RESTORED {TARGET_SCHEMA}.{table}: "
            f"{restored:,} rows"
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# COMMAND LINE
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "action",
        choices=[
            "compare",
            "load",
            "verify",
            "restore",
        ],
    )

    parser.add_argument(
        "--tables",
        nargs="+",
        choices=list(config()),
        default=None,
    )

    parser.add_argument(
        "--output",
        default="nesna_pilot_results.json",
    )

    parser.add_argument(
        "--no-prod",
        action="store_true",
    )

    parser.add_argument(
        "--deep-verify",
        choices=[
            "full",
            "counts",
        ],
        default="full",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
    )

    parser.add_argument(
        "--confirm",
    )

    parser.add_argument(
        "--expect-dev-db",
    )

    parser.add_argument(
        "--expect-dev-server-ip",
    )

    parser.add_argument(
        "--backup-run-id",
    )

    parser.add_argument(
        "--approved-preflight",
    )

    args = parser.parse_args()

    if args.tables is None:
        if args.action in ["load", "restore"]:
            raise SystemExit(
                "--tables is mandatory for writes"
            )

        args.tables = list(config())

    if len(set(args.tables)) != len(args.tables):
        raise SystemExit(
            "Duplicate table selection"
        )

    if args.action == "compare":
        cmd_compare(args)

    elif args.action == "load":
        cmd_load(args)

    elif args.action == "verify":
        cmd_verify(args)

    elif args.action == "restore":
        cmd_restore(args)


if __name__ == "__main__":
    main()