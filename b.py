import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, text

# ============================================================
# POSTGRESQL CONNECTION CONFIGURATION
# Keep the same working values from your original script.
# ============================================================

SQL_USERNAME = "python_user"
SQL_PASSWORD = "PASTE_YOUR_EXISTING_PASSWORD"

DEV_HOST = "18.210.201.107"
DEV_PORT = 5432
DEV_DATABASE = "warehouse"

PROD_HOST = "3.221.58.110"
PROD_PORT = 5432
PROD_DATABASE = "warehouse"

# ============================================================
# DISCOVERY CONFIGURATION
# ============================================================

RAW_SCHEMA = "raw"
DEV_TARGET_SCHEMA = "nesna"
PROD_TARGET_SCHEMA = "nesna"

EXPECTED_TABLE_COUNT = 66

OUTPUT_DIR = Path("results")


# ============================================================
# CONNECTIONS — SAME IMPLEMENTATION AS ORIGINAL
# ============================================================

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
    if SQL_PASSWORD in (
        "YOUR_ACTUAL_PASSWORD",
        "PASTE_YOUR_EXISTING_PASSWORD",
    ):
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
# READ-ONLY SQL
# ============================================================

TABLES_SQL = text("""
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = :schema
      AND table_type = 'BASE TABLE'
    ORDER BY table_name
""")

COLUMNS_SQL = text("""
    SELECT
        column_name,
        data_type,
        udt_name,
        character_maximum_length,
        numeric_precision,
        numeric_scale,
        datetime_precision,
        is_nullable,
        ordinal_position,
        column_default,
        is_identity,
        is_generated
    FROM information_schema.columns
    WHERE table_schema = :schema
      AND table_name = :table
    ORDER BY ordinal_position
""")

MAPPINGS_SQL = text("""
    SELECT ms, pg, fields
    FROM staging.masterstaging
    ORDER BY ms, pg
""")


# ============================================================
# METADATA
# ============================================================

def get_tables(connection, schema):
    return [
        row[0]
        for row in connection.execute(
            TABLES_SQL,
            {"schema": schema},
        )
    ]


def get_columns(connection, schema, table):
    return [
        dict(row)
        for row in connection.execute(
            COLUMNS_SQL,
            {"schema": schema, "table": table},
        ).mappings()
    ]


def unique_casefold(items, description):
    lookup = defaultdict(list)

    for item in items:
        lookup[item.casefold()].append(item)

    duplicates = {
        key: values
        for key, values in lookup.items()
        if len(values) != 1
    }

    if duplicates:
        raise ValueError(
            f"Ambiguous {description}: {duplicates}"
        )

    return {
        key: values[0]
        for key, values in lookup.items()
    }


def column_signature(column):
    keys = (
        "column_name",
        "data_type",
        "udt_name",
        "character_maximum_length",
        "numeric_precision",
        "numeric_scale",
        "datetime_precision",
        "is_nullable",
        "ordinal_position",
        "column_default",
        "is_identity",
        "is_generated",
    )

    return tuple(column.get(key) for key in keys)


# ============================================================
# STRICT SOURCE TABLE RESOLUTION
# ============================================================

def sqlserver_parts(name):
    """
    Accept only schema.table SQL Server names.

    Example:
        perm.Contract

    Do not guess using table names alone.
    """

    if not isinstance(name, str):
        raise ValueError("Mapping ms must be text.")

    cleaned = (
        name.strip()
        .replace("[", "")
        .replace("]", "")
        .replace('"', "")
    )

    parts = cleaned.split(".")

    valid = all(
        re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*",
            part,
        )
        for part in parts
    )

    if len(parts) != 2 or not valid:
        raise ValueError(
            f"Expected schema.table, got {name!r}"
        )

    return parts


def resolve_raw(source_name, raw_lookup):
    """
    Strictly map:

        perm.Contract
            ->
        raw."perm__Contract"

    Never fall back to an unrelated Contract table.
    """

    schema, table = sqlserver_parts(source_name)

    expected = f"{schema}__{table}"

    actual = raw_lookup.get(expected.casefold())

    return actual, expected


# ============================================================
# MAPPING EXPRESSION PARSER
# ============================================================

def split_fields(fields):
    """
    Split comma-separated SQL projections without splitting
    commas inside functions, parentheses, or quoted strings.
    """

    if not isinstance(fields, str) or not fields.strip():
        raise ValueError(
            "Mapping fields must be a nonempty string."
        )

    result = []
    current = []

    depth = 0
    quote_char = None
    bracket = False

    i = 0

    while i < len(fields):
        ch = fields[i]

        if quote_char:
            current.append(ch)

            if ch == quote_char:
                if (
                    i + 1 < len(fields)
                    and fields[i + 1] == quote_char
                ):
                    current.append(fields[i + 1])
                    i += 1
                else:
                    quote_char = None

        elif bracket:
            current.append(ch)

            if ch == "]":
                bracket = False

        elif ch in ('"', "'"):
            quote_char = ch
            current.append(ch)

        elif ch == "[":
            bracket = True
            current.append(ch)

        elif ch == "(":
            depth += 1
            current.append(ch)

        elif ch == ")":
            depth -= 1

            if depth < 0:
                raise ValueError(
                    "Unbalanced mapping parentheses."
                )

            current.append(ch)

        elif ch == "," and depth == 0:
            item = "".join(current).strip()

            if not item:
                raise ValueError(
                    "Empty mapping projection."
                )

            result.append(item)
            current = []

        else:
            current.append(ch)

        i += 1

    if depth or quote_char or bracket:
        raise ValueError(
            "Unbalanced mapping expression."
        )

    item = "".join(current).strip()

    if not item:
        raise ValueError(
            "Empty final mapping projection."
        )

    result.append(item)

    return result


def parse_projection(expression):
    """
    Supported:
      ColumnName
      [ColumnName]
      "ColumnName"
      ColumnName AS Alias
      CAST(ColumnName AS INT) AS Alias

    Unsupported expressions are BLOCKED, not guessed.
    """

    expression = expression.strip()

    ident = (
        r'(?:'
        r'\[([A-Za-z_][A-Za-z0-9_]*)\]'
        r'|"([A-Za-z_][A-Za-z0-9_]*)"'
        r'|([A-Za-z_][A-Za-z0-9_]*)'
        r')'
    )

    simple = re.fullmatch(
        rf"\s*{ident}"
        rf"(?:\s+(?:AS\s+)?{ident})?"
        rf"\s*",
        expression,
        flags=re.IGNORECASE,
    )

    if simple:
        source = next(
            value
            for value in simple.group(1, 2, 3)
            if value is not None
        )

        target = next(
            (
                value
                for value in simple.group(4, 5, 6)
                if value is not None
            ),
            source,
        )

        return {
            "source": source,
            "target": target,
            "transform": None,
            "expression": expression,
        }

    cast = re.fullmatch(
        rf"\s*CAST\s*\(\s*{ident}"
        rf"\s+AS\s+(INT|INTEGER)\s*\)"
        rf"(?:\s+(?:AS\s+)?{ident})?"
        rf"\s*",
        expression,
        flags=re.IGNORECASE,
    )

    if cast:
        source = next(
            value
            for value in cast.group(1, 2, 3)
            if value is not None
        )

        target = next(
            (
                value
                for value in cast.group(5, 6, 7)
                if value is not None
            ),
            source,
        )

        return {
            "source": source,
            "target": target,
            "transform": "integer",
            "expression": expression,
        }

    raise ValueError(
        f"Unsupported mapping expression: {expression!r}"
    )


# ============================================================
# INSPECT ONE MAPPING
# ============================================================

def inspect_mapping(
    mapping,
    raw_lookup,
    dev_lookup,
    prod_lookup,
    dev_connection,
    prod_connection,
):
    result = {
        "mapping_ms": mapping["ms"],
        "mapping_pg": mapping["pg"],
        "status": "BLOCKED",
        "errors": [],
        "warnings": [],
        "selected_columns": [],
        "surplus_raw_columns": [],
    }

    try:
        source_name = mapping["ms"]
        target_name = mapping["pg"]

        if (
            not isinstance(target_name, str)
            or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*",
                target_name.strip(),
            )
        ):
            raise ValueError(
                f"Invalid destination name: {target_name!r}"
            )

        target_name = target_name.strip()

        raw_name, expected_raw = resolve_raw(
            source_name,
            raw_lookup,
        )

        dev_target = dev_lookup.get(
            target_name.casefold()
        )

        prod_target = prod_lookup.get(
            target_name.casefold()
        )

        result["expected_raw_table"] = expected_raw
        result["raw_table"] = raw_name
        result["dev_nesna_table"] = dev_target
        result["prod_nesna_table"] = prod_target

        if not raw_name:
            result["errors"].append(
                f"Missing exact RAW table: {expected_raw}"
            )

        if not dev_target:
            result["errors"].append(
                f"Missing DEV NESNA table: {target_name}"
            )

        if not prod_target:
            result["errors"].append(
                f"Missing PROD NESNA table: {target_name}"
            )

        if result["errors"]:
            return result

        raw_columns = get_columns(
            dev_connection,
            RAW_SCHEMA,
            raw_name,
        )

        dev_columns = get_columns(
            dev_connection,
            DEV_TARGET_SCHEMA,
            dev_target,
        )

        prod_columns = get_columns(
            prod_connection,
            PROD_TARGET_SCHEMA,
            prod_target,
        )

        result["raw_column_count"] = len(raw_columns)
        result["dev_nesna_column_count"] = len(dev_columns)
        result["prod_nesna_column_count"] = len(prod_columns)

        dev_signature = [
            column_signature(column)
            for column in dev_columns
        ]

        prod_signature = [
            column_signature(column)
            for column in prod_columns
        ]

        schema_match = dev_signature == prod_signature

        result[
            "dev_prod_column_definition_match"
        ] = schema_match

        if not schema_match:
            result["errors"].append(
                "DEV/PROD NESNA column definitions differ."
            )

        raw_column_lookup = unique_casefold(
            [
                column["column_name"]
                for column in raw_columns
            ],
            "RAW columns",
        )

        dev_column_lookup = unique_casefold(
            [
                column["column_name"]
                for column in dev_columns
            ],
            "DEV NESNA columns",
        )

        prod_column_lookup = unique_casefold(
            [
                column["column_name"]
                for column in prod_columns
            ],
            "PROD NESNA columns",
        )

        projections = [
            parse_projection(expression)
            for expression in split_fields(
                mapping["fields"]
            )
        ]

        seen_targets = set()
        selected_sources = set()

        for projection in projections:
            source_key = projection["source"].casefold()
            target_key = projection["target"].casefold()

            source_column = raw_column_lookup.get(
                source_key
            )

            dev_column = dev_column_lookup.get(
                target_key
            )

            prod_column = prod_column_lookup.get(
                target_key
            )

            if not source_column:
                result["errors"].append(
                    f"Missing RAW column: "
                    f"{projection['source']}"
                )

            if not dev_column or not prod_column:
                result["errors"].append(
                    f"Missing DEV/PROD destination column: "
                    f"{projection['target']}"
                )

            if target_key in seen_targets:
                result["errors"].append(
                    f"Duplicate destination projection: "
                    f"{projection['target']}"
                )

            seen_targets.add(target_key)

            if source_column:
                selected_sources.add(
                    source_column.casefold()
                )

            projection[
                "resolved_source"
            ] = source_column

            projection[
                "resolved_target"
            ] = dev_column

            result["selected_columns"].append(
                projection
            )

        # Every destination column must have a mapping.
        missing_targets = (
            set(dev_column_lookup) - seen_targets
        )

        if missing_targets:
            names = sorted(
                dev_column_lookup[key]
                for key in missing_targets
            )

            result["errors"].append(
                "Unmapped destination columns: "
                + ", ".join(names)
            )

        surplus = [
            column["column_name"]
            for column in raw_columns
            if column["column_name"].casefold()
            not in selected_sources
        ]

        result["surplus_raw_columns"] = surplus

        result["selected_column_count"] = len(
            projections
        )

        result["surplus_raw_column_count"] = len(
            surplus
        )

        # No actual data conversion or loading is attempted.
        result["warnings"].append(
            "Metadata-only discovery. Actual cast compatibility, "
            "row values, constraints, foreign keys, and DAG "
            "dependencies require separate validation."
        )

        if not result["errors"]:
            result["status"] = "MAPPING_CANDIDATE"

    except Exception as exc:
        result["errors"].append(
            f"{type(exc).__name__}: {exc}"
        )

    return result


# ============================================================
# OPTIONAL EXACT DAG 1 INVENTORY
# ============================================================

def load_inventory(path):
    data = json.loads(
        Path(path).read_text(encoding="utf-8")
    )

    if (
        not isinstance(data, list)
        or not all(
            isinstance(value, str)
            for value in data
        )
    ):
        raise ValueError(
            "Inventory must be a JSON array "
            "of exact RAW table names."
        )

    normalized = [
        name.casefold()
        for name in data
    ]

    if len(normalized) != len(set(normalized)):
        raise ValueError(
            "Inventory contains duplicate table names."
        )

    if len(data) != EXPECTED_TABLE_COUNT:
        raise ValueError(
            f"Inventory contains {len(data)} tables; "
            f"expected {EXPECTED_TABLE_COUNT}."
        )

    return data


# ============================================================
# MAIN DISCOVERY
# ============================================================

def compare(args):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    dev_engine, prod_engine = connect_databases()

    report = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "mode": "READ_ONLY_DISCOVERY",
        "expected_table_count": EXPECTED_TABLE_COUNT,
        "inventory_provided": bool(args.inventory),
        "tables": [],
        "global_errors": [],
        "global_warnings": [],
    }

    try:
        with (
            dev_engine.connect() as dev,
            prod_engine.connect() as prod,
        ):
            # Enforce read-only transactions.
            dev.execute(
                text("SET TRANSACTION READ ONLY")
            )

            prod.execute(
                text("SET TRANSACTION READ ONLY")
            )

            raw_names = get_tables(
                dev,
                RAW_SCHEMA,
            )

            dev_names = get_tables(
                dev,
                DEV_TARGET_SCHEMA,
            )

            prod_names = get_tables(
                prod,
                PROD_TARGET_SCHEMA,
            )

            raw_lookup = unique_casefold(
                raw_names,
                "RAW tables",
            )

            dev_lookup = unique_casefold(
                dev_names,
                "DEV NESNA tables",
            )

            prod_lookup = unique_casefold(
                prod_names,
                "PROD NESNA tables",
            )

            report["raw_table_count"] = len(
                raw_names
            )

            report["dev_nesna_table_count"] = len(
                dev_names
            )

            report["prod_nesna_table_count"] = len(
                prod_names
            )

            mappings = [
                dict(row)
                for row in dev.execute(
                    MAPPINGS_SQL
                ).mappings()
            ]

            report[
                "masterstaging_mapping_rows"
            ] = len(mappings)

            if not mappings:
                report["global_errors"].append(
                    "staging.masterstaging returned "
                    "zero mappings."
                )

            by_destination = defaultdict(list)
            by_source = defaultdict(list)

            for mapping in mappings:
                ms = mapping["ms"]
                pg = mapping["pg"]

                if (
                    not isinstance(ms, str)
                    or not isinstance(pg, str)
                ):
                    report["global_errors"].append(
                        f"Invalid mapping row: {mapping!r}"
                    )
                    continue

                by_destination[
                    pg.strip().casefold()
                ].append(mapping)

                by_source[
                    ms.strip().casefold()
                ].append(mapping)

            # Detect duplicate destination mappings.
            for key, group in by_destination.items():
                if len(group) > 1:
                    report["global_errors"].append(
                        f"Duplicate destination mapping "
                        f"{key}: {len(group)} rows."
                    )

            # Detect one source mapped to multiple targets.
            for key, group in by_source.items():
                if len(group) > 1:
                    destinations = [
                        str(row["pg"])
                        for row in group
                    ]

                    report["global_errors"].append(
                        f"Source {key} maps to multiple "
                        f"destinations: {destinations}"
                    )

            # Prefer the actual DAG 1 inventory.
            if args.inventory:
                inventory = load_inventory(
                    args.inventory
                )
            else:
                inventory = raw_names

                report["global_warnings"].append(
                    "No DAG 1 inventory supplied. "
                    "Auditing every visible RAW base table. "
                    "Cannot certify these are exactly "
                    "the 66 DAG 1 tables."
                )

            if len(inventory) != EXPECTED_TABLE_COUNT:
                report["global_errors"].append(
                    f"Audited inventory contains "
                    f"{len(inventory)} tables; "
                    f"expected {EXPECTED_TABLE_COUNT}."
                )

            inventory_lookup = unique_casefold(
                inventory,
                "inventory tables",
            )

            for name in inventory:
                if name.casefold() not in raw_lookup:
                    report["global_errors"].append(
                        f"Inventory RAW table missing: {name}"
                    )

            # Associate mapping rows with exact RAW names.
            matching_rows = defaultdict(list)

            for mapping in mappings:
                try:
                    _, expected = resolve_raw(
                        mapping["ms"],
                        raw_lookup,
                    )

                    if (
                        expected.casefold()
                        in inventory_lookup
                    ):
                        matching_rows[
                            expected.casefold()
                        ].append(mapping)

                except Exception as exc:
                    report["global_warnings"].append(
                        f"Unresolvable mapping "
                        f"{mapping['ms']!r}: {exc}"
                    )

            # Inspect every inventory table.
            for name in inventory:
                candidates = matching_rows.get(
                    name.casefold(),
                    [],
                )

                if len(candidates) != 1:
                    status = (
                        "MISSING_MAPPING"
                        if not candidates
                        else "AMBIGUOUS_MAPPING"
                    )

                    report["tables"].append({
                        "raw_table": name,
                        "status": status,
                        "errors": [
                            f"Expected exactly one mapping; "
                            f"found {len(candidates)}."
                        ],
                        "mapping_candidates": [
                            {
                                "ms": row["ms"],
                                "pg": row["pg"],
                            }
                            for row in candidates
                        ],
                    })

                    continue

                result = inspect_mapping(
                    candidates[0],
                    raw_lookup,
                    dev_lookup,
                    prod_lookup,
                    dev,
                    prod,
                )

                report["tables"].append(result)

    finally:
        dev_engine.dispose()
        prod_engine.dispose()

    # ========================================================
    # FINAL STATUS
    # ========================================================

    statuses = Counter(
        row["status"]
        for row in report["tables"]
    )

    report["status_counts"] = dict(statuses)

    report["ready_count"] = statuses[
        "MAPPING_CANDIDATE"
    ]

    report["all_66_mapping_candidates"] = (
        len(report["tables"]) == EXPECTED_TABLE_COUNT
        and report["ready_count"] == EXPECTED_TABLE_COUNT
        and not report["global_errors"]
        and bool(args.inventory)
    )

    # ========================================================
    # JSON REPORT
    # ========================================================

    json_path = (
        OUTPUT_DIR / "nesna_66_discovery.json"
    )

    json_path.write_text(
        json.dumps(
            report,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    # ========================================================
    # CSV REPORT
    # ========================================================

    csv_path = (
        OUTPUT_DIR / "nesna_66_summary.csv"
    )

    fieldnames = [
        "raw_table",
        "mapping_ms",
        "mapping_pg",
        "status",
        "raw_column_count",
        "selected_column_count",
        "surplus_raw_column_count",
        "dev_nesna_column_count",
        "prod_nesna_column_count",
        "dev_prod_column_definition_match",
        "errors",
        "surplus_raw_columns",
    ]

    with csv_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for row in report["tables"]:
            output_row = {}

            for key in fieldnames:
                value = row.get(key, "")

                if isinstance(value, list):
                    value = " | ".join(
                        str(item)
                        for item in value
                    )

                output_row[key] = value

            writer.writerow(output_row)

    # ========================================================
    # CONSOLE OUTPUT
    # ========================================================

    print("\n" + "=" * 100)
    print("NESNA 66-TABLE DISCOVERY — READ ONLY")
    print("=" * 100)

    print(
        f"RAW tables visible: "
        f"{report['raw_table_count']}"
    )

    print(
        f"Masterstaging mappings: "
        f"{report['masterstaging_mapping_rows']}"
    )

    print(
        f"Mapping candidates: "
        f"{report['ready_count']}/{EXPECTED_TABLE_COUNT}"
    )

    print(
        f"Status counts: "
        f"{report['status_counts']}"
    )

    print(
        f"DAG 1 inventory supplied: "
        f"{report['inventory_provided']}"
    )

    print(
        f"All 66 mapping candidates: "
        f"{report['all_66_mapping_candidates']}"
    )

    print("\n" + "-" * 100)

    for row in report["tables"]:
        raw_table = str(
            row.get("raw_table", "?")
        )

        target = str(
            row.get("mapping_pg", "?")
        )

        status = row["status"]

        print(
            f"\nRAW: {raw_table}"
            f"\nNESNA: {target}"
            f"\nSTATUS: {status}"
        )

        print(
            "Columns:",
            {
                "raw": row.get(
                    "raw_column_count"
                ),
                "selected": row.get(
                    "selected_column_count"
                ),
                "surplus": row.get(
                    "surplus_raw_column_count"
                ),
                "dev_nesna": row.get(
                    "dev_nesna_column_count"
                ),
                "prod_nesna": row.get(
                    "prod_nesna_column_count"
                ),
            },
        )

        print(
            "DEV/PROD definitions match:",
            row.get(
                "dev_prod_column_definition_match"
            ),
        )

        for error in row.get("errors", []):
            print("ERROR:", error)

    print("\n" + "=" * 100)

    for error in report["global_errors"]:
        print("GLOBAL ERROR:", error)

    for warning in report["global_warnings"]:
        print("WARNING:", warning)

    print("\nReports generated:")
    print(json_path)
    print(csv_path)

    print(
        "\nNOTE: MAPPING_CANDIDATE means the "
        "metadata checks passed. It does not "
        "authorize loading."
    )

    return (
        0
        if report["all_66_mapping_candidates"]
        else 2
    )


# ============================================================
# COMMAND LINE
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only NESNA 66-table mapping discovery"
        )
    )

    parser.add_argument(
        "--inventory",
        help=(
            "Optional JSON file containing the exact "
            "66 RAW table names from DAG 1."
        ),
    )

    args = parser.parse_args()

    raise SystemExit(compare(args))


if __name__ == "__main__":
    main()