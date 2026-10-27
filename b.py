import argparse
import csv
import getpass
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


from urllib.parse import quote
from sqlalchemy import create_engine

# ============================================================
# POSTGRESQL CONNECTION CONFIGURATION
# ============================================================

SQL_USERNAME = "python_user"

SQL_PASSWORD = "pgpyusr"

# DEV PostgreSQL
DEV_HOST = "18.210.201.107" 
DEV_PORT = 5432
DEV_DATABASE = "warehouse"

# PROD PostgreSQL
PROD_HOST = "3.221.58.110"
PROD_PORT = 5432
PROD_DATABASE = "warehouse"

# ============================================================
# NESNA MIGRATION CONFIGURATION
# ============================================================

# DEV source: already ingested SQL Server data
RAW_SCHEMA = "raw"

# Existing NESNA destination tables
DEV_TARGET_SCHEMA = "nesna"
PROD_TARGET_SCHEMA = "nesna"

# Backup location — DEV ONLY
BACKUP_SCHEMA = "nesna_dev_migration_backup"

# Five-table pilot
PILOT_TABLES = {
    "permcontract",
    "permcontractfield",
    "permcontractdisbursement",
    "permledger",
    "permnote",
}

# Local comparison reports
OUTPUT_DIR = Path("results")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def make_engine(host, port, database):
    encoded_password = quote(SQL_PASSWORD, safe="")
    encoded_username = quote(SQL_USERNAME, safe="")

    connection_string = (
        f"postgresql+psycopg2://"
        f"{encoded_username}:{encoded_password}"
        f"@{host}:{port}/{database}"
    )

    return create_engine(
        connection_string,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 15},
    )


def connect_databases():
    if SQL_PASSWORD == "YOUR_ACTUAL_PASSWORD":
        raise RuntimeError("Configure SQL_PASSWORD first.")

    if DEV_HOST == "YOUR_DEV_HOST":
        raise RuntimeError("Configure DEV_HOST first.")

    if PROD_HOST == "YOUR_PROD_HOST":
        raise RuntimeError("Configure PROD_HOST first.")

    if (
        DEV_HOST == PROD_HOST
        and DEV_PORT == PROD_PORT
        and DEV_DATABASE == PROD_DATABASE
    ):
        raise RuntimeError(
            "DEV and PROD cannot point to the same database."
        )

    dev = make_engine(
        DEV_HOST,
        DEV_PORT,
        DEV_DATABASE,
    )

    prod = make_engine(
        PROD_HOST,
        PROD_PORT,
        PROD_DATABASE,
    )

    return dev, prod


# ============================================================
# SAFE SQL IDENTIFIERS
# ============================================================

def identifier(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value}")

    return '"' + value.replace('"', '""') + '"'


def table_sql(schema, table):
    return f"{identifier(schema)}.{identifier(table)}"


# ============================================================
# DATABASE METADATA
# ============================================================

def get_columns(connection, schema, table):
    query = text("""
        SELECT
            column_name,
            data_type,
            udt_name,
            character_maximum_length,
            numeric_precision,
            numeric_scale,
            is_nullable,
            ordinal_position
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
        ORDER BY ordinal_position
    """)

    rows = connection.execute(
        query,
        {"schema": schema, "table": table},
    ).mappings().all()

    return [dict(row) for row in rows]


def get_tables(connection, schema):
    query = text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = :schema
          AND table_type = 'BASE TABLE'
    """)

    return {
        row[0].lower(): row[0]
        for row in connection.execute(
            query,
            {"schema": schema},
        )
    }


def get_count(connection, schema, table):
    query = text(
        f"SELECT COUNT(*) FROM {table_sql(schema, table)}"
    )

    return connection.execute(query).scalar_one()


def schema_signature(columns):
    return [
        (
            column["column_name"].lower(),
            column["data_type"],
            column["udt_name"],
            column["character_maximum_length"],
            column["numeric_precision"],
            column["numeric_scale"],
            column["is_nullable"],
        )
        for column in columns
    ]


# ============================================================
# INDIA'S MASTERSTAGING MAPPINGS
# ============================================================

def get_mappings(connection):
    query = text("""
        SELECT ms, pg, fields
        FROM staging.masterstaging
        WHERE LOWER(pg) IN (
            'permcontract',
            'permcontractfield',
            'permcontractdisbursement',
            'permledger',
            'permnote'
        )
    """)

    rows = connection.execute(query).mappings().all()

    mappings = {}

    for row in rows:
        destination = row["pg"].lower()

        if destination in mappings:
            raise RuntimeError(
                f"Duplicate masterstaging mapping: {destination}"
            )

        mappings[destination] = {
            "source": row["ms"],
            "fields": row["fields"],
        }

    missing = PILOT_TABLES - set(mappings)

    if missing:
        raise RuntimeError(
            f"Missing masterstaging mappings: {sorted(missing)}"
        )

    return mappings


# ============================================================
# SOURCE DISCOVERY
# ============================================================

def normalize_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def discover_source_table(connection, source_name):
    raw_tables = get_tables(connection, RAW_SCHEMA)

    source_parts = (
        source_name.replace("[", "")
        .replace("]", "")
        .split(".")
    )

    candidates = [
        source_name,
        source_name.replace(".", "_"),
        source_name.replace(".", ""),
        source_parts[-1],
    ]

    for candidate in candidates:
        actual = raw_tables.get(candidate.lower())

        if actual:
            return actual

    normalized = {
        normalize_name(candidate)
        for candidate in candidates
    }

    matches = [
        actual
        for actual in raw_tables.values()
        if normalize_name(actual) in normalized
    ]

    if len(matches) == 1:
        return matches[0]

    raise RuntimeError(
        f"Cannot uniquely identify DEV raw table "
        f"for {source_name}. Matches: {matches}"
    )


# ============================================================
# MAPPING FIELD PARSER
# ============================================================

def split_fields(fields):
    if not fields:
        raise ValueError("Empty mapping fields.")

    result = []
    buffer = []
    depth = 0

    for character in fields:
        if character == "(":
            depth += 1

        elif character == ")":
            depth -= 1

        if depth < 0:
            raise ValueError("Invalid mapping parentheses.")

        if character == "," and depth == 0:
            result.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(character)

    if depth != 0:
        raise ValueError("Invalid mapping parentheses.")

    result.append("".join(buffer).strip())

    return result


def parse_field(expression):
    expression = expression.strip()

    # Simple column, optionally bracketed.
    simple = re.fullmatch(
        r"\[?([A-Za-z_][A-Za-z0-9_]*)\]?",
        expression,
    )

    if simple:
        name = simple.group(1)

        return {
            "source": name,
            "target": name,
            "cast": None,
        }

    # CAST(RemovedFromSource AS INT) AS RemovedFromSource
    cast = re.fullmatch(
        r"CAST\s*\(\s*"
        r"\[?([A-Za-z_][A-Za-z0-9_]*)\]?"
        r"\s+AS\s+INT(?:EGER)?\s*\)"
        r"\s+(?:AS\s+)?"
        r"\[?([A-Za-z_][A-Za-z0-9_]*)\]?",
        expression,
        flags=re.IGNORECASE,
    )

    if cast:
        return {
            "source": cast.group(1),
            "target": cast.group(2),
            "cast": "integer",
        }

    raise RuntimeError(
        f"Unsupported mapping expression: {expression}. "
        "Refusing to guess."
    )


def parse_mapping(fields):
    return [
        parse_field(field)
        for field in split_fields(fields)
    ]


# ============================================================
# COMPARE
# ============================================================

def inspect_table(dev_connection, prod_connection, target, mapping):
    source = discover_source_table(
        dev_connection,
        mapping["source"],
    )

    raw_columns = get_columns(
        dev_connection,
        RAW_SCHEMA,
        source,
    )

    dev_columns = get_columns(
        dev_connection,
        DEV_TARGET_SCHEMA,
        target,
    )

    prod_columns = get_columns(
        prod_connection,
        PROD_TARGET_SCHEMA,
        target,
    )

    if not raw_columns:
        raise RuntimeError(f"Missing DEV raw table: {source}")

    if not dev_columns:
        raise RuntimeError(f"Missing DEV nesna table: {target}")

    if not prod_columns:
        raise RuntimeError(f"Missing PROD nesna table: {target}")

    parsed_fields = parse_mapping(mapping["fields"])

    raw_lookup = {
        column["column_name"].lower(): column["column_name"]
        for column in raw_columns
    }

    target_lookup = {
        column["column_name"].lower(): column["column_name"]
        for column in dev_columns
    }

    selected = []

    for field in parsed_fields:
        source_column = raw_lookup.get(
            field["source"].lower()
        )

        target_column = target_lookup.get(
            field["target"].lower()
        )

        if not source_column:
            raise RuntimeError(
                f"{target}: source column missing: "
                f"{field['source']}"
            )

        if not target_column:
            raise RuntimeError(
                f"{target}: destination column missing: "
                f"{field['target']}"
            )

        selected.append({
            "source": source_column,
            "target": target_column,
            "cast": field["cast"],
        })

    selected_sources = {
        field["source"].lower()
        for field in selected
    }

    surplus = [
        column["column_name"]
        for column in raw_columns
        if column["column_name"].lower()
        not in selected_sources
    ]

    schema_matches_prod = (
        schema_signature(dev_columns)
        == schema_signature(prod_columns)
    )

    return {
        "target": target,
        "source": source,
        "mapping_source": mapping["source"],
        "selected_columns": selected,
        "surplus_raw_columns": surplus,
        "raw_column_count": len(raw_columns),
        "required_column_count": len(selected),
        "dev_nesna_column_count": len(dev_columns),
        "prod_nesna_column_count": len(prod_columns),
        "dev_prod_schema_match": schema_matches_prod,
        "raw_rows": get_count(
            dev_connection,
            RAW_SCHEMA,
            source,
        ),
        "dev_nesna_rows": get_count(
            dev_connection,
            DEV_TARGET_SCHEMA,
            target,
        ),
        "prod_nesna_rows": get_count(
            prod_connection,
            PROD_TARGET_SCHEMA,
            target,
        ),
    }


def compare(dev, prod):
    with dev.connect() as dev_connection:
        with prod.connect() as prod_connection:

            mappings = get_mappings(dev_connection)

            report = {
                "generated_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "tables": [],
            }

            for target in sorted(PILOT_TABLES):
                print(f"\nInspecting {target}...")

                result = inspect_table(
                    dev_connection,
                    prod_connection,
                    target,
                    mappings[target],
                )

                report["tables"].append(result)

                print(
                    "DEV/PROD schema match:",
                    result["dev_prod_schema_match"],
                )

                print(
                    "RAW columns:",
                    result["raw_column_count"],
                )

                print(
                    "Required columns:",
                    result["required_column_count"],
                )

                print(
                    "Surplus RAW columns:",
                    result["surplus_raw_columns"],
                )

                print(
                    "Rows:",
                    {
                        "raw": result["raw_rows"],
                        "dev_nesna": result["dev_nesna_rows"],
                        "prod_nesna": result["prod_nesna_rows"],
                    },
                )

    output = OUTPUT_DIR / "comparison.json"

    output.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )

    print(f"\nComparison saved: {output}")

    return report


# ============================================================
# DEV-ONLY LOAD
# ============================================================

def load_one(dev, prod, target):
    if target not in PILOT_TABLES:
        raise RuntimeError("Target is not in the pilot allowlist.")

    # Explicit guard against pointing DEV at the PROD endpoint.
    if (
        DEV_HOST.strip().lower() == PROD_HOST.strip().lower()
        and DEV_PORT == PROD_PORT
        and DEV_DATABASE == PROD_DATABASE
    ):
        raise RuntimeError(
            "DEV and PROD point to the same database endpoint."
        )

    # Read-only preflight before any DEV modification.
    with dev.connect() as dev_connection:
        with prod.connect() as prod_connection:
            mappings = get_mappings(dev_connection)

            inspection = inspect_table(
                dev_connection,
                prod_connection,
                target,
                mappings[target],
            )

    if not inspection["dev_prod_schema_match"]:
        raise RuntimeError(
            "DEV nesna does not match PROD nesna. "
            "Load blocked."
        )

    selected = inspection["selected_columns"]

    if not selected:
        raise RuntimeError("No mapped columns. Load blocked.")

    if len({
        field["target"].lower()
        for field in selected
    }) != len(selected):
        raise RuntimeError(
            "Duplicate destination columns. Load blocked."
        )

    source = inspection["source"]

    source_table = table_sql(RAW_SCHEMA, source)
    target_table = table_sql(DEV_TARGET_SCHEMA, target)

    projections = []

    for field in selected:
        source_name = identifier(field["source"])
        target_name = identifier(field["target"])

        if field["cast"] == "integer":
            expression = f"{source_name}::integer"
        else:
            expression = source_name

        projections.append(
            f"{expression} AS {target_name}"
        )

    target_columns = ", ".join(
        identifier(field["target"])
        for field in selected
    )

    select_columns = ", ".join(
        identifier(field["target"])
        for field in selected
    )

    projection_sql = ", ".join(projections)

    backup_name = (
        f"{target}_"
        f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
    )

    backup_table = table_sql(
        BACKUP_SCHEMA,
        backup_name,
    )

    print("\nDEV LOAD PLAN")
    print("Source:", source_table)
    print("Destination:", target_table)
    print("Backup:", backup_table)
    print("Selected columns:", target_columns)
    print("Source rows:", inspection["raw_rows"])
    print("Existing DEV rows:", inspection["dev_nesna_rows"])

    confirmation = input(
        "\nType REPLACE_DEV_NESNA to continue: "
    )

    if confirmation != "REPLACE_DEV_NESNA":
        print("Cancelled. No changes made.")
        return

    # All changes happen in a single DEV transaction.
    # Failure rolls back the replacement.
    with dev.begin() as connection:

        connection.execute(text(
            f"CREATE SCHEMA IF NOT EXISTS "
            f"{identifier(BACKUP_SCHEMA)}"
        ))

        connection.execute(text(
            f"CREATE TABLE {backup_table} AS "
            f"SELECT * FROM {target_table}"
        ))

        connection.execute(text(
            f"CREATE TEMP TABLE nesna_pilot_stage AS "
            f"SELECT {projection_sql} "
            f"FROM {source_table}"
        ))

        staged_rows = connection.execute(
            text(
                "SELECT COUNT(*) "
                "FROM nesna_pilot_stage"
            )
        ).scalar_one()

        connection.execute(text(
            f"TRUNCATE TABLE {target_table}"
        ))

        connection.execute(text(
            f"INSERT INTO {target_table} "
            f"({target_columns}) "
            f"SELECT {select_columns} "
            f"FROM nesna_pilot_stage"
        ))

        loaded_rows = connection.execute(
            text(
                f"SELECT COUNT(*) FROM {target_table}"
            )
        ).scalar_one()

        if staged_rows != loaded_rows:
            raise RuntimeError(
                "Row-count verification failed. "
                "Transaction will roll back."
            )

        # Exact multiset comparison, including duplicate rows.
        difference = connection.execute(text(
            f"""
            SELECT EXISTS (
                (
                    SELECT {target_columns}
                    FROM {target_table}
                    EXCEPT ALL
                    SELECT {select_columns}
                    FROM nesna_pilot_stage
                )
                UNION ALL
                (
                    SELECT {select_columns}
                    FROM nesna_pilot_stage
                    EXCEPT ALL
                    SELECT {target_columns}
                    FROM {target_table}
                )
            )
            """
        )).scalar_one()

        if difference:
            raise RuntimeError(
                "Exact data verification failed. "
                "Transaction will roll back."
            )

        print(
            f"\nSUCCESS: {loaded_rows} rows "
            f"loaded into DEV {target_table}"
        )

        print("Backup:", backup_table)

    print("DEV transaction committed.")


# ============================================================
# COMMAND LINE
# ============================================================

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "command",
        choices=["compare", "load"],
    )

    parser.add_argument(
        "--table",
        choices=sorted(PILOT_TABLES),
    )

    args = parser.parse_args()

    if args.command == "load" and not args.table:
        parser.error("load requires --table")

    dev, prod = connect_databases()

    try:
        if args.command == "compare":
            compare(dev, prod)

        elif args.command == "load":
            load_one(dev, prod, args.table)

    finally:
        dev.dispose()
        prod.dispose()


if __name__ == "__main__":
    main()
    
    
    
    #python .\nesna_dev_pilot.py compare
    
    
    
    #python .\nesna_dev_pilot.py load --table permcontract
    
    