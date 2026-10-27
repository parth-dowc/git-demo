from sqlalchemy import create_engine, text
from urllib.parse import quote

# ============================================================
# CONNECTION CONFIGURATION
# ============================================================

SQL_USERNAME = "python_user"
SQL_PASSWORD = "YOUR_PASSWORD"

DEV_HOST = "YOUR_DEV_HOST"
PROD_HOST = "YOUR_PROD_HOST"

DATABASE = "warehouse"

TABLES = {
    "permcontract": "perm__Contract",
    "permcontractdisbursement": "perm__ContractDisbursement",
    "permledger": "perm__Ledger",
    "permnote": "perm__Note",
}


def connect(host):
    password = quote(SQL_PASSWORD, safe="")
    username = quote(SQL_USERNAME, safe="")

    return create_engine(
        f"postgresql+psycopg2://"
        f"{username}:{password}@{host}:5432/{DATABASE}"
    )


def get_columns(conn, schema, table):
    sql = text("""
        SELECT
            column_name,
            data_type,
            udt_name,
            character_maximum_length,
            numeric_precision,
            numeric_scale,
            is_nullable
        FROM information_schema.columns
        WHERE table_schema = :schema
          AND table_name = :table
        ORDER BY ordinal_position
    """)

    return [
        dict(row)
        for row in conn.execute(
            sql,
            {"schema": schema, "table": table},
        ).mappings()
    ]


def signature(column):
    return (
        column["column_name"].lower(),
        column["data_type"],
        column["udt_name"],
        column["character_maximum_length"],
        column["numeric_precision"],
        column["numeric_scale"],
        column["is_nullable"],
    )


def main():
    dev_engine = connect(DEV_HOST)
    prod_engine = connect(PROD_HOST)

    try:
        with dev_engine.connect() as dev:
            with prod_engine.connect() as prod:

                print("\n" + "=" * 100)
                print("NESNA FOUR-TABLE COLUMN VALIDATION")
                print("=" * 100)

                all_pass = True

                for target, source in TABLES.items():
                    raw = get_columns(dev, "raw", source)
                    dev_nesna = get_columns(dev, "nesna", target)
                    prod_nesna = get_columns(prod, "nesna", target)

                    if not raw or not dev_nesna or not prod_nesna:
                        print(f"\n{target}: FAIL — Missing table or columns")
                        all_pass = False
                        continue

                    raw_names = {
                        c["column_name"].lower()
                        for c in raw
                    }

                    prod_names = {
                        c["column_name"].lower()
                        for c in prod_nesna
                    }

                    surplus = sorted(raw_names - prod_names)

                    dev_sig = [signature(c) for c in dev_nesna]
                    prod_sig = [signature(c) for c in prod_nesna]

                    match = dev_sig == prod_sig

                    if not match:
                        all_pass = False

                    print(f"\nTABLE: {target}")
                    print("-" * 100)

                    print(f"DEV RAW columns:       {len(raw)}")
                    print(f"DEV NESNA columns:     {len(dev_nesna)}")
                    print(f"PROD NESNA columns:    {len(prod_nesna)}")

                    print(f"RAW surplus columns:   {len(surplus)}")
                    print(f"RAW surplus names:     {surplus}")

                    print(
                        "DEV/PROD NESNA MATCH:  ",
                        "PASS" if match else "FAIL",
                    )

                print("\n" + "=" * 100)
                print(
                    "FINAL RESULT:",
                    "PASS — ALL FOUR TABLES MATCH PROD"
                    if all_pass
                    else "FAIL — REVIEW DIFFERENCES",
                )
                print("=" * 100)

    finally:
        dev_engine.dispose()
        prod_engine.dispose()


if __name__ == "__main__":
    main()