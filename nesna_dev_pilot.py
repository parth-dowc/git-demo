#!/usr/bin/env python3
"""NESNA five-table DEV-only raw -> public pilot.

Commands:
  compare    READ ONLY: DEV raw vs DEV public vs PROD public (read-only connection)
  load       DEV ONLY: backup, stage, TRUNCATE/INSERT, exact verification, commit
  verify     READ ONLY: DEV public vs DEV raw projection and PROD public
  restore    DEV ONLY: restore a previously backed-up public table

Install: pip install 'psycopg2-binary>=2.9'
Configure DSNs using secure environment variables or libpq service files.
NO credentials, SQL Server access, or raw_nesna schema used.
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
from urllib.parse import quote
from sqlalchemy import create_engine
import psycopg2

# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

# --------------------------------------------------
# DEV
# --------------------------------------------------

DEV_HOST = "18.210.201.107"
DEV_DATABASE = "warehouse"

# --------------------------------------------------
# PROD
# --------------------------------------------------

PROD_HOST = "3.221.58.110"
PROD_DATABASE = "warehouse"

# --------------------------------------------------
# SHARED
# --------------------------------------------------

DB_USER = "python_user"
DB_PASSWORD = "pgpyusr"

ENCODED_PASSWORD = quote(DB_PASSWORD, safe="")


DEV_CONNECTION_STRING = (
    f"postgresql+psycopg2://"
    f"{DB_USER}:{ENCODED_PASSWORD}"
    f"@{DEV_HOST}/{DEV_DATABASE}"
)

PROD_CONNECTION_STRING = (
    f"postgresql+psycopg2://"
    f"{DB_USER}:{ENCODED_PASSWORD}"
    f"@{PROD_HOST}/{PROD_DATABASE}"
)

dev_engine = create_engine(DEV_CONNECTION_STRING)
prod_engine = create_engine(PROD_CONNECTION_STRING)

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'five_tables.json'
BACKUP_SCHEMA = 'nesna_dev_migration_backup'
FIELD_SIMPLE = re.compile(r'^(?:\[([A-Za-z][A-Za-z_0-9]*)\]|([A-Za-z][A-Za-z_0-9]*))$')
FIELD_CAST_INT = re.compile(
    r'^CAST\s*\(\s*(?:\[([A-Za-z][A-Za-z_0-9]*)\]|([A-Za-z][A-Za-z_0-9]*))\s+AS\s+INT\s*\)\s+(?:AS\s+)?([A-Za-z][A-Za-z_0-9]*)$', re.I)


def psy():
    try:
        import psycopg2
        from psycopg2 import sql
        return psycopg2, sql
    except ImportError as exc:
        raise SystemExit("Install psycopg2-binary in the Python environment executing these scripts") from exc


def config():
    specs = json.loads(CONFIG.read_text())['tables']
    if len(specs) != 5 or len({x['public'] for x in specs}) != 5:
        raise RuntimeError('Pilot configuration must contain exactly five unique targets')
    return {x['public']: x for x in specs}


def source_key(source):
    parts = source.replace('[', '').replace(']', '').split('.')
    if len(parts) != 2 or any(not re.fullmatch(r'[A-Za-z][A-Za-z_0-9]*', x) for x in parts):
        raise ValueError(f'Unsafe/invalid masterstaging source identifier: {source!r}')
    return tuple(x.casefold() for x in parts)


def split_tsql_fields(s):
    """Split selected T-SQL expressions without splitting function arguments."""
    out, buf, depth, bracket, quoted, pos = [], [], 0, False, False, 0
    while pos < len(s):
        ch = s[pos]
        nxt = s[pos + 1] if pos + 1 < len(s) else ''
        if quoted:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    pos += 1
                else:
                    quoted = False
        elif bracket:
            buf.append(ch)
            if ch == ']':
                if nxt == ']':
                    buf.append(nxt)
                    pos += 1
                else:
                    bracket = False
        elif ch == "'":
            quoted = True
            buf.append(ch)
        elif ch == '[':
            bracket = True
            buf.append(ch)
        elif ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            if depth < 0:
                raise ValueError('Unbalanced parentheses in masterstaging.fields')
            buf.append(ch)
        elif ch == ',' and depth == 0:
            out.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        pos += 1
    if bracket or quoted or depth != 0:
        raise ValueError('Unbalanced masterstaging.fields')
    out.append(''.join(buf).strip())
    if not all(out):
        raise ValueError('Empty expression in masterstaging.fields')
    return out


def parse_expression(text):
    text = text.strip()
    match = FIELD_SIMPLE.fullmatch(text)
    if match:
        src = match.group(1) or match.group(2)
        return ('column', src, src)
    match = FIELD_CAST_INT.fullmatch(text)
    if match:
        src = match.group(1) or match.group(2)
        return ('int', src, match.group(3))
    raise ValueError('Unsupported field expression: ' + text)


def find_unique(name, names, context):
    hits = [x for x in names if x.casefold() == name.casefold()]
    if len(hits) != 1:
        raise ValueError(f'{context}: expected one match for {name!r}; got {hits!r}')
    return hits[0]


def raw_name_for(source, available):
    schema, table = source_key(source)
    names = {schema + table, schema + '_' + table, schema + '__' + table, schema + '.' + table}
    hits = [name for name in available if name.casefold() in names]
    if len(hits) != 1:
        raise ValueError(f'raw source for {source}: ambiguous/not found among naming conventions: {hits!r}. Must explicitly resolve before writing.')
    return hits[0]


def qtable(sql, schema, table):
    return sql.SQL('{}.{}').format(sql.Identifier(schema), sql.Identifier(table))


def tables_in(cur, schema):
    cur.execute('''SELECT c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON c.relnamespace=n.oid
        WHERE n.nspname=%s AND c.relkind='r' AND NOT c.relispartition''', (schema,))
    return [r[0] for r in cur.fetchall()]


def cols(cur, schema, table):
    cur.execute('''SELECT a.attname, format_type(a.atttypid,a.atttypmod) AS type,
        a.attnotnull, a.attgenerated, a.attidentity,
        pg_get_expr(d.adbin,d.adrelid) AS def,
        a.attcollation::oid
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE n.nspname=%s AND c.relname=%s
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum''', (schema, table))
    return [dict(name=x[0], type=x[1], not_null=x[2], generated=x[3], identity=x[4], default=x[5], collation=x[6]) for x in cur.fetchall()]


def structural_signature(cur, table):
    cur.execute('''SELECT c.oid::text, c.relkind, c.relispartition,
      c.relrowsecurity, c.relforcerowsecurity,
      pg_get_userbyid(c.relowner), c.relacl::text,
      coalesce((SELECT jsonb_agg(pg_get_constraintdef(con.oid) ORDER BY con.conname)::text
                FROM pg_constraint con WHERE con.conrelid=c.oid),'[]'),
      coalesce((SELECT jsonb_agg(pg_get_indexdef(idx.indexrelid) ORDER BY idx.indexrelid)::text
                FROM pg_index idx WHERE idx.indrelid=c.oid),'[]'),
      coalesce((SELECT jsonb_agg(pg_get_triggerdef(tr.oid) ORDER BY tr.tgname)::text
                FROM pg_trigger tr WHERE tr.tgrelid=c.oid AND NOT tr.tgisinternal),'[]')
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public' AND c.relname=%s''', (table,))
    head = cur.fetchone()
    return (head, cols(cur, 'public', table))


def public_hazards(cur, table):
    cur.execute('''SELECT c.oid, c.relkind, c.relispartition, c.relrowsecurity,
        c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=%s''', (table,))
    rec = cur.fetchone()
    if not rec or rec[1] != 'r' or rec[2] or rec[3] or rec[4]:
        raise ValueError(f'Public target {table}: missing, partitioned, or RLS-enabled')
    oid = rec[0]
    cur.execute("SELECT conname FROM pg_constraint WHERE contype='f' AND confrelid=%s", (oid,))
    fks = [x[0] for x in cur.fetchall()]
    if fks:
        raise ValueError(f'{table}: inbound foreign keys make independent TRUNCATE unsafe: {fks}')
    cur.execute('SELECT tgname FROM pg_trigger WHERE tgrelid=%s AND NOT tgisinternal', (oid,))
    triggers = [x[0] for x in cur.fetchall()]
    if triggers:
        raise ValueError(f'{table}: noninternal triggers require review: {triggers}')
    cur.execute('SELECT rulename FROM pg_rules WHERE schemaname=%s AND tablename=%s', ('public',table))
    if cur.fetchall():
        raise ValueError(f'{table}: public rewrite rules require review')


def build_projection(sql, specs, raw_cols, public_cols):
    actual = [c['name'] for c in public_cols]
    parsed = [parse_expression(s) for s in specs]
    outputs = [output for _, _, output in parsed]
    if [s.casefold() for s in outputs] != [s.casefold() for s in actual]:
        raise ValueError(f'Public column ORDER/NAMES differ: projected {outputs}, actual {actual}')
    if len({x.casefold() for x in outputs}) != len(outputs):
        raise ValueError('Duplicate projected field names')
    if any(c['identity'] or c['generated'] for c in public_cols):
        raise ValueError('Identity or generated public columns need a separate approved strategy')
    raw_names = [c['name'] for c in raw_cols]
    pieces = []
    for kind, source, out in parsed:
        actual_source = find_unique(source, raw_names, 'DEV raw column')
        expression = sql.Identifier(actual_source)
        if kind == 'int':
            expression = sql.SQL('({})::integer').format(expression)
        pieces.append(sql.SQL('{} AS {}').format(expression, sql.Identifier(find_unique(out, actual, 'public column'))))
    return sql.SQL(', ').join(pieces), [c['name'] for c in public_cols]


def live_masterstaging(cur, spec):
    cur.execute('SELECT ms, pg, fields FROM staging.masterstaging')
    matches = [(ms, pg, fields) for ms, pg, fields in cur.fetchall()
               if source_key(ms) == source_key(spec['source']) and str(pg).casefold() == spec['public'].casefold()]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one LIVE masterstaging entry for {spec['source']} -> {spec['public']}; found {len(matches)}")
    parsed_live = [parse_expression(s) for s in split_tsql_fields(matches[0][2])]
    parsed_config = [parse_expression(s) for s in spec['fields']]
    normalize = lambda seq: [(kind, src.casefold(), out.casefold()) for kind, src, out in seq]
    if normalize(parsed_live) != normalize(parsed_config):
        raise ValueError(f"Live masterstaging.fields changed for {spec['public']}: migration blocked")
    return matches[0][2]


def discover(cur, spec, sql):
    live_masterstaging(cur, spec)
    raw_name = raw_name_for(spec['source'], tables_in(cur, 'raw'))
    public_name = find_unique(spec['public'], tables_in(cur, 'public'), 'DEV public table')
    raw_columns = cols(cur, 'raw', raw_name)
    public_columns = cols(cur, 'public', public_name)
    public_hazards(cur, public_name)
    exprs, column_names = build_projection(sql, spec['fields'], raw_columns, public_columns)
    key = spec['key']
    if key is not None:
        find_unique(key, column_names, 'Pilot key')
    return dict(spec=spec, raw_name=raw_name, public_name=public_name,
                raw_cols=raw_columns, public_cols=public_columns,
                expressions=exprs, names=column_names)


def count(cur, sql, schema, table):
    cur.execute(sql.SQL('SELECT count(*) FROM {}').format(qtable(sql,schema,table)))
    return cur.fetchone()[0]


def duplicates(cur, sql, obj):
    key = obj['spec']['key']
    if key is None:
        return None
    col = find_unique(key, obj['names'], 'Key column')
    query = sql.SQL('''SELECT EXISTS (
        SELECT 1 FROM {} GROUP BY {} HAVING count(*)>1 LIMIT 1
        )''').format(qtable(sql, 'raw', obj['raw_name']), sql.Identifier(col))
    cur.execute(query)
    return cur.fetchone()[0]


def report_row(cur, obj, sql, prod_cur=None):
    raw = obj['raw_name']
    dst = obj['public_name']
    raw_count = count(cur,sql,'raw',raw)
    public_dev_count = count(cur,sql,'public',dst)
    prod_count = None
    prod_col = None
    prod_match = None
    if prod_cur is not None:
        prod_rel = find_unique(dst, tables_in(prod_cur,'public'), 'PROD public table')
        prod_col = cols(prod_cur,'public',prod_rel)
        prod_count = count(prod_cur,sql,'public',prod_rel)
        sig = lambda li: [(x['name'],x['type'],x['not_null']) for x in li]
        prod_match = sig(prod_col) == sig(obj['public_cols'])
    required_sources = {parse_expression(f)[1].casefold() for f in obj['spec']['fields']}
    raw_extra = [x['name'] for x in obj['raw_cols'] if x['name'].casefold() not in required_sources]
    diff = (100.0*(raw_count-prod_count)/prod_count if prod_count else None)
    return dict(generated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                source=obj['spec']['source'], raw=f'raw.{raw}', public=f'public.{dst}',
                source_fields=len(obj['spec']['fields']),raw_columns=len(obj['raw_cols']),
                public_dev_columns=len(obj['public_cols']), public_prod_columns=len(prod_col) if prod_col else None,
                dev_vs_prod_schema_exact=prod_match, unused_raw_columns=raw_extra,
                dev_raw_rows=raw_count, dev_public_before_rows=public_dev_count,
                prod_public_rows=prod_count,dev_raw_vs_prod_pct=round(diff,3) if diff is not None else None)


def summary_files(records, outfile):
    path = Path(outfile)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(records,indent=2,default=str))
    if isinstance(records,list):
        csv_file = path.with_suffix('.csv')
        cols_out = ['source','raw','public','raw_columns','source_fields','public_dev_columns',
                    'public_prod_columns','dev_vs_prod_schema_exact','dev_raw_rows',
                    'dev_public_before_rows','prod_public_rows','dev_raw_vs_prod_pct']
        with csv_file.open('w',newline='') as out:
            writer=csv.DictWriter(out,fieldnames=cols_out,extrasaction='ignore')
            writer.writeheader()
            writer.writerows(records)
        print('CSV:', csv_file)
    print('JSON:',path)


def connect_dev(readonly):
    pg, _ = psy()

    conn = pg.connect(
        host=DEV_HOST,
        dbname=DEV_DATABASE,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=15,
        application_name="nesna_dev_pilot_dev",
    )

    conn.set_session(
        readonly=readonly,
        isolation_level="REPEATABLE READ",
        autocommit=False,
    )

    return conn


def connect_prod(readonly):
    pg, _ = psy()

    conn = pg.connect(
        host=PROD_HOST,
        dbname=PROD_DATABASE,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=15,
        application_name="nesna_dev_pilot_prod",
    )

    conn.set_session(
        readonly=readonly,
        isolation_level="REPEATABLE READ",
        autocommit=False,
    )

    return conn


def identity(cur):
    cur.execute('SELECT current_database(), inet_server_addr()::text, inet_server_port(), current_user')
    return cur.fetchone()


def assert_dev_identity(cur, args):
    db, host, port, role = identity(cur)
    if not args.expect_dev_db or not args.expect_dev_server_ip:
        raise ValueError('DEV writes require --expect-dev-db AND --expect-dev-server-ip')
    if (db,host) != (args.expect_dev_db,args.expect_dev_server_ip):
        raise ValueError(f'WRITE BLOCKED: connected database/server {(db,host)!r} do not match expected DEV identity')
    if db.casefold().endswith('_prod') or 'production' in db.casefold():
        raise ValueError('PROD-labelled DB blocked')
    return dict(database=db,server_ip=host,port=port,role=role)


def deep_exact_equal(cur, sql, left, right, names):
    """TRUE multiset equality; EXCEPT ALL detects missing, extra and duplicate rows.
    Deliberately expensive for millions of rows, but stronger than equal counts.
    """
    projection=sql.SQL(', ').join(sql.Identifier(x) for x in names)
    for a,b in [(left,right),(right,left)]:
        stmt=sql.SQL('SELECT EXISTS(SELECT 1 FROM (SELECT {} FROM {} EXCEPT ALL SELECT {} FROM {}) delta LIMIT 1)').format(
            projection,a,projection,b)
        cur.execute(stmt)
        if cur.fetchone()[0]:
            raise RuntimeError('Full row-multiset equality test FAILED after publication')


def perform_load(conn, sql, obj, args, run_id):
    target = qtable(sql,'public',obj['public_name'])
    source = qtable(sql,'raw',obj['raw_name'])
    stage = sql.Identifier('nesna_dev_stage')
    cols_sql = sql.SQL(', ').join(sql.Identifier(x) for x in obj['names'])
    backup_name = f'b_{run_id}_{obj["public_name"]}'
    if len(backup_name.encode())>63:
        raise ValueError('Backup table name exceeds PostgreSQL limit')
    backup=qtable(sql,BACKUP_SCHEMA,backup_name)
    with conn.cursor() as cur:
        assert_dev_identity(cur,args)
        # One pilot table per transaction; caller commits on success.
        cur.execute('SET LOCAL lock_timeout = %s', ('15s',))
        cur.execute('SET LOCAL statement_timeout = %s', ('12h',))
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s),hashtext(%s))', ('nesna_raw_to_public_dev',obj['public_name']))
        live = discover(cur,obj['spec'],sql)
        if live['raw_name'] != obj['raw_name'] or live['public_name'] != obj['public_name']:
            raise RuntimeError('Mapping changed after preflight')
        old_sig=structural_signature(cur,obj['public_name'])
        # Populate stage from an MVCC-consistent snapshot of DEV raw.
        cur.execute(sql.SQL('CREATE TEMP TABLE {} (LIKE {} INCLUDING CONSTRAINTS) ON COMMIT DROP').format(stage,target))
        cur.execute(sql.SQL('INSERT INTO {} ({}) SELECT {} FROM {}').format(
            stage,cols_sql,obj['expressions'],source))
        stage_inserted=cur.rowcount
        cur.execute(sql.SQL('SELECT count(*) FROM {}').format(stage))
        staged=cur.fetchone()[0]
        raw_rows=count(cur,sql,'raw',obj['raw_name'])
        if staged != stage_inserted or staged != raw_rows:
            raise RuntimeError(f'Pre-publication count mismatch: raw={raw_rows}, stage={staged}, inserted={stage_inserted}')
        cur.execute(sql.SQL('SELECT EXISTS(SELECT 1 FROM {} LIMIT 1)').format(target))
        had_rows=cur.fetchone()[0]
        if staged==0 and had_rows:
            raise RuntimeError('Zero-row raw source with populated public target: refusing replacement')
        # Blocks concurrent legacy writers BEFORE taking a complete pre-change backup.
        # Coordinate a DEV ETL pause; lock timeout prevents indefinite waiting.
        cur.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE').format(target))
        cur.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(BACKUP_SCHEMA)))
        # Refuse an existing backup table name rather than overwrite.
        cur.execute('SELECT to_regclass(%s)',(f'{BACKUP_SCHEMA}.{backup_name}',))
        if cur.fetchone()[0] is not None:
            raise RuntimeError(f'Backup already exists: {BACKUP_SCHEMA}.{backup_name}')
        cur.execute(sql.SQL('CREATE TABLE {} AS SELECT {} FROM {}').format(backup,cols_sql,target))
        previous=cur.rowcount
        if previous != count(cur,sql,BACKUP_SCHEMA,backup_name):
            raise RuntimeError('Pre-change backup count did not reconcile')
        # Reject drift and force a rollback on any changed structure.
        if structural_signature(cur,obj['public_name']) != old_sig:
            raise RuntimeError('DEV public schema changed before publication')
        cur.execute(sql.SQL('TRUNCATE TABLE {}').format(target))  # no CASCADE
        cur.execute(sql.SQL('INSERT INTO {} ({}) SELECT {} FROM {}').format(target,cols_sql,cols_sql,stage))
        published = cur.rowcount
        if published != staged or count(cur,sql,'public',obj['public_name']) != staged:
            raise RuntimeError('Published row count mismatch')
        if args.deep_verify == 'full':
            deep_exact_equal(cur,sql,stage,target,obj['names'])
        if structural_signature(cur,obj['public_name']) != old_sig:
            raise RuntimeError('Schema changed during publication')
        return dict(status='SUCCESS',raw=f'raw.{obj["raw_name"]}',public=f'public.{obj["public_name"]}',
                    backup=f'{BACKUP_SCHEMA}.{backup_name}',before_rows=previous,stage_rows=staged,
                    published_rows=published,deep_verification=args.deep_verify)


def cmd_compare(args):
    _, sql = psy()
    specs = config()
    dev = connect_dev(True)
    prod = connect_prod(True)
    conn = connect_dev(False)
    report = []
    try:
        with dev.cursor() as dc:
            dev_ident = identity(dc)
            print('DEV identity:',dev_ident)
            if prod:
                with prod.cursor() as pc:
                    prod_ident = identity(pc)
                    print('PROD identity (read-only):',prod_ident)
                    if dev_ident[:3] == prod_ident[:3]:
                        raise RuntimeError('PROD_DSN points to the SAME backend database as DEV_DSN: comparison blocked')
                    for t in args.tables:
                        obj=discover(dc,specs[t],sql)
                        row=report_row(dc,obj,sql,pc)
                        row['dev_identity'] = list(dev_ident[:3])
                        row['prod_identity'] = list(prod_ident[:3])
                        report.append(row)
                        print(f"{t}: raw={row['dev_raw_rows']:,} DEV public={row['dev_public_before_rows']:,} PROD public={row['prod_public_rows']:,} schema_match={row['dev_vs_prod_schema_exact']}")
            else:
                for t in args.tables:
                    obj=discover(dc,specs[t],sql)
                    row=report_row(dc,obj,sql)
                    report.append(row)
                    print(f"{t}: raw={row['dev_raw_rows']:,} DEV public={row['dev_public_before_rows']:,} (PROD not checked)")
        summary_files(report,args.output)
    finally:
        dev.close()
        if prod: prod.close()


def cmd_verify(args):
    # Does not mutate DEV or PROD.
    _, sql = psy()
    specs = config()
    dev = connect(os.environ.get('NESNA_DEV_DSN'),True)
    prod = connect(os.environ.get('NESNA_PROD_DSN'),True) if not args.no_prod else None
    records=[]
    try:
        with dev.cursor() as dc:
            if prod:
                pc=prod.cursor()
                if identity(dc)[:3] == identity(pc)[:3]:
                    raise RuntimeError('PROD_DSN and DEV_DSN resolve to the same database')
            else:
                pc=None
            try:
                for t in args.tables:
                    obj=discover(dc,specs[t],sql)
                    row=report_row(dc,obj,sql,pc)
                    projected=sql.SQL('(SELECT {} FROM {}) AS projected_raw').format(obj['expressions'],qtable(sql,'raw',obj['raw_name']))
                    target=qtable(sql,'public',obj['public_name'])
                    row['dev_public_matches_raw_count']=(row['dev_raw_rows']==row['dev_public_before_rows'])
                    if args.deep_verify=='full':
                        deep_exact_equal(dc,sql,projected,target,obj['names'])
                        row['dev_public_exact_raw_rows']=True
                    else:
                        row['dev_public_exact_raw_rows']='NOT TESTED'
                    if row['dev_vs_prod_schema_exact'] is False:
                        raise RuntimeError(f'{t}: DEV/PROD public schema mismatch')
                    if not row['dev_public_matches_raw_count']:
                        raise RuntimeError(f'{t}: DEV raw/public count mismatch')
                    records.append(row)
                    print(f'{t}: PASSED')
            finally:
                if pc: pc.close()
        summary_files(records,args.output)
    finally:
        dev.close()
        if prod: prod.close()


def cmd_load(args):
    _, sql = psy()
    specs=config()
    if not args.apply or args.confirm != 'TRUNCATE_DEV_PUBLIC_ONLY':
        raise SystemExit('WRITE BLOCKED: require --apply --confirm TRUNCATE_DEV_PUBLIC_ONLY')
    if os.getenv('NESNA_DEV_WRITES_ENABLED') != 'YES':
        raise SystemExit('WRITE BLOCKED: export NESNA_DEV_WRITES_ENABLED=YES')
    if not args.expect_dev_db or not args.expect_dev_server_ip:
        raise SystemExit('WRITE BLOCKED: --expect-dev-db and --expect-dev-server-ip are required')
    if not args.tables:
        raise SystemExit('WRITE BLOCKED: explicitly select one or more --tables')
    if not args.approved_preflight:
        raise SystemExit('WRITE BLOCKED: require --approved-preflight from the PROD/DEV read-only comparison')
    approved = json.loads(Path(args.approved_preflight).read_text())
    for table in args.tables:
        matches = [x for x in approved if x.get('public') == 'public.' + table]
        if len(matches) != 1 or matches[0].get('dev_vs_prod_schema_exact') is not True:
            raise SystemExit(f'WRITE BLOCKED: no exact DEV/PROD public schema match in preflight for {table}')
        if not matches[0].get('prod_identity'):
            raise SystemExit(f'WRITE BLOCKED: preflight for {table} did not actually compare against PROD')
        if matches[0].get('dev_identity', [])[:2] != [args.expect_dev_db,args.expect_dev_server_ip]:
            raise SystemExit(f'WRITE BLOCKED: preflight DEV identity mismatch for {table}')
        when = dt.datetime.fromisoformat(matches[0]['generated_at_utc'])
        if dt.datetime.now(dt.timezone.utc) - when > dt.timedelta(hours=24):
            raise SystemExit(f'WRITE BLOCKED: preflight for {table} is older than 24 hours')
    run_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + uuid.uuid4().hex[:5]
    results=[]
    for table in args.tables:
        conn=connect(os.environ.get('NESNA_DEV_DSN'),False)
        try:
            with conn.cursor() as cur:
                assert_dev_identity(cur,args)
                obj=discover(cur,specs[table],sql)
            result=perform_load(conn,sql,obj,args,run_id)
            conn.commit()
            results.append(result)
            print(f"COMMITTED {table}: {result['published_rows']:,} rows; backup={result['backup']}")
        except Exception as exc:
            conn.rollback()
            results.append(dict(status='FAILED',public='public.'+table,error=str(exc)))
            print(f'FAILED {table}: {exc}; this table rolled back. Earlier commits remain.',file=sys.stderr)
            break
        finally:
            conn.close()
            summary_files(results,args.output)
    if len(results)!=len(args.tables) or any(r['status']!='SUCCESS' for r in results):
        raise SystemExit(1)
    print('All requested tables committed. Preserve backups and run read-only verify.')


def cmd_restore(args):
    """Restore a single, previously committed DEV table from its backup.
    Usage intentionally requires a second distinct confirmation token.
    """
    _, sql=psy()
    if not args.apply or args.confirm!='RESTORE_DEV_PUBLIC_ONLY' or os.getenv('NESNA_DEV_WRITES_ENABLED')!='YES':
        raise SystemExit('Restore requires --apply --confirm RESTORE_DEV_PUBLIC_ONLY and NESNA_DEV_WRITES_ENABLED=YES')
    if len(args.tables)!=1 or not args.backup_run_id:
        raise SystemExit('Restore requires exactly one --tables and --backup-run-id')
    if not re.fullmatch(r'\d{8}T\d{6}[a-f0-9]{5}', args.backup_run_id):
        raise SystemExit('Invalid run ID')
    table=args.tables[0]
    backup_name=f'b_{args.backup_run_id}_{table}'
    conn=connect(os.environ.get('NESNA_DEV_DSN'),False)
    try:
        with conn.cursor() as cur:
            assert_dev_identity(cur,args)
            public_name=find_unique(table,tables_in(cur,'public'),'Restore target')
            backup_name=find_unique(backup_name,tables_in(cur,BACKUP_SCHEMA),'Restore backup')
            public_cols=cols(cur,'public',public_name)
            backup_cols=cols(cur,BACKUP_SCHEMA,backup_name)
            signature=lambda cs:[(c['name'],c['type'],c['not_null']) for c in cs]
            # CTAS does not always retain NOT NULL metadata; original column names/types must match.
            if [(c['name'],c['type']) for c in public_cols] != [(c['name'],c['type']) for c in backup_cols]:
                raise RuntimeError('Backup columns/types differ from destination')
            public_hazards(cur,public_name)
            prior=structural_signature(cur,public_name)
            cur.execute('SET LOCAL lock_timeout=%s',('15s',))
            cur.execute(sql.SQL('LOCK TABLE {} IN SHARE MODE').format(qtable(sql,'public',public_name)))
            restored_count=count(cur,sql,BACKUP_SCHEMA,backup_name)
            dest=qtable(sql,'public',public_name)
            backup=qtable(sql,BACKUP_SCHEMA,backup_name)
            colsql=sql.SQL(', ').join(sql.Identifier(c['name']) for c in public_cols)
            cur.execute(sql.SQL('TRUNCATE TABLE {}').format(dest))
            cur.execute(sql.SQL('INSERT INTO {} ({}) SELECT {} FROM {}').format(dest,colsql,colsql,backup))
            if cur.rowcount != restored_count or count(cur,sql,'public',public_name)!=restored_count:
                raise RuntimeError('Restore row count mismatch')
            if args.deep_verify=='full':
                deep_exact_equal(cur,sql,dest,backup,[c['name'] for c in public_cols])
            if structural_signature(cur,public_name)!=prior:
                raise RuntimeError('Unexpected schema change during restore')
        conn.commit()
        print(f'RESTORED public.{table} with {restored_count:,} original rows')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action',choices=['compare','load','verify','restore'])
    ap.add_argument('--tables',nargs='+',choices=list(config()),default=None,
                    help='One or more exact public names. compare/verify default to five; LOAD requires explicit names.')
    ap.add_argument('--output',default='nesna_pilot_results.json')
    ap.add_argument('--no-prod',action='store_true',help='Skip PROD entirely for read-only comparison/verify')
    ap.add_argument('--deep-verify',choices=['full','counts'],default='full')
    ap.add_argument('--apply',action='store_true')
    ap.add_argument('--confirm')
    ap.add_argument('--expect-dev-db')
    ap.add_argument('--expect-dev-server-ip')
    ap.add_argument('--backup-run-id')
    ap.add_argument('--approved-preflight',help='Required for LOAD: recent read-only comparison JSON with matching DEV/PROD public schema')
    args=ap.parse_args()
    if args.tables is None:
        if args.action in ('load','restore'):
            raise SystemExit('--tables is mandatory for any write')
        args.tables=list(config())
    if len(set(args.tables)) != len(args.tables):
        raise SystemExit('Duplicate table in --tables')
    if args.action=='compare': cmd_compare(args)
    elif args.action=='verify': cmd_verify(args)
    elif args.action=='load': cmd_load(args)
    else: cmd_restore(args)

if __name__=='__main__':
    main()
