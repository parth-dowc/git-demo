-- NESNA PILOT 5: RUN ON THE DEV WAREHOUSE. READ ONLY.
-- DEV raw schema = raw. DEV legacy destinations = public.
-- Raw physical relation names may be quoted/mixed-case; discover them here.

SELECT current_database() AS database_name,
       inet_server_addr()::text AS server_ip,
       inet_server_port() AS server_port,
       current_user AS db_user;

-- India's exact live projections and legacy physical public names.
SELECT ms AS india_sqlserver_source,
       pg AS india_postgres_public_table,
       fields AS india_original_selected_fields,
       batch, sort
FROM staging.masterstaging
WHERE lower(pg) IN (
  'permcontract',
  'permcontractfield',
  'permcontractdisbursement',
  'permledger',
  'permnote'
)
ORDER BY pg;

-- Inspect raw and public catalog names without relying on guessed raw naming.
SELECT n.nspname AS schema_name,
       c.relname AS physical_table_name,
       c.reltuples::bigint AS estimated_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS disk_size
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname IN ('raw','public') AND c.relkind IN ('r','p')
  AND (
     (n.nspname='public' AND lower(c.relname) IN
      ('permcontract','permcontractfield','permcontractdisbursement','permledger','permnote'))
     OR (n.nspname='raw' AND
         (lower(c.relname) LIKE 'perm%contract%'
          OR lower(c.relname) LIKE 'perm%ledger%'
          OR lower(c.relname) LIKE 'perm%note%'))
  )
ORDER BY n.nspname,c.relname;

-- Exact existing DEV public column contract, in physical ordinal order.
SELECT c.relname AS public_table, a.attnum AS ordinal,
       a.attname AS column_name,
       pg_catalog.format_type(a.atttypid,a.atttypmod) AS pg_type,
       a.attnotnull AS not_null
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
WHERE n.nspname='public' AND lower(c.relname) IN
 ('permcontract','permcontractfield','permcontractdisbursement','permledger','permnote')
 AND a.attnum>0 AND NOT a.attisdropped
ORDER BY c.relname,a.attnum;
