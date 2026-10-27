-- ============================================================
-- NESNA DEV — FOUR-TABLE POPULATION
--
-- SOURCE:      DEV raw
-- DESTINATION: DEV nesna
--
-- PROD:        NEVER ACCESSED
--
-- Tables:
--   1. permcontract
--   2. permcontractdisbursement
--   3. permledger
--   4. permnote
--
-- permcontractfield intentionally excluded.
--
-- This script replaces existing DEV destination rows.
-- No destination columns or table definitions are changed.
-- ============================================================


BEGIN;

-- ------------------------------------------------------------
-- SAFETY CHECK: ENSURE WE ARE CONNECTED TO DEV
-- ------------------------------------------------------------

DO $$
BEGIN
    IF current_database() <> 'warehouse' THEN
        RAISE EXCEPTION
            'ABORTED: Expected warehouse database, connected to %',
            current_database();
    END IF;

    IF inet_server_addr() IS DISTINCT FROM
       '18.210.201.107'::inet THEN
        RAISE EXCEPTION
            'ABORTED: This is not the expected DEV PostgreSQL server. Actual: %',
            inet_server_addr();
    END IF;

    RAISE NOTICE 'DEV database identity verified.';
END
$$;


-- ------------------------------------------------------------
-- 1. CONTRACT
-- ------------------------------------------------------------

TRUNCATE TABLE nesna.permcontract;

INSERT INTO nesna.permcontract (
    "ContractId",
    "EarningMethodInd",
    "fxCoBuyerId",
    "fxCustomerId",
    "fxSoldByUserId",
    externaluser
)
SELECT
    "ContractId",
    "EarningMethodInd",
    "fxCoBuyerId",
    "fxCustomerId",
    "fxSoldByUserId",
    "ExternalUser"
FROM raw."perm__Contract";


-- ------------------------------------------------------------
-- 2. CONTRACT DISBURSEMENT
-- ------------------------------------------------------------

TRUNCATE TABLE nesna.permcontractdisbursement;

INSERT INTO nesna.permcontractdisbursement (
    "ContractDisbursementId",
    "fxContractId",
    "DisbursementTypeInd",
    "DisbursementRefundTypeInd",
    "fxDisbursementGroupId",
    "fxDisbursementCodeId",
    "SourceDescription",
    "fxDisbursementLabelId"
)
SELECT
    "ContractDisbursementId",
    "fxContractId",
    "DisbursementTypeInd",
    "DisbursementRefundTypeInd",
    "fxDisbursementGroupId",
    "fxDisbursementCodeId",
    "SourceDescription",
    "fxDisbursementLabelId"
FROM raw."perm__ContractDisbursement";


-- ------------------------------------------------------------
-- 3. LEDGER
-- ------------------------------------------------------------

TRUNCATE TABLE nesna.permledger;

INSERT INTO nesna.permledger (
    "LedgerId",
    "fxRelatedContractId"
)
SELECT
    "LedgerId",
    "fxRelatedContractId"
FROM raw."perm__Ledger";


-- ------------------------------------------------------------
-- 4. NOTE
-- ------------------------------------------------------------

TRUNCATE TABLE nesna.permnote;

INSERT INTO nesna.permnote (
    "NoteId",
    "RemovedFromSource",
    "fxAccountHolderId",
    "fxLedgerId",
    "fxClaimId",
    "Note",
    "fxEnteredById",
    "EnteredDate",
    "NoteTypeInd",
    "WarningNoteLevelInd",
    "ExpirationDate"
)
SELECT
    "NoteId",
    "RemovedFromSource"::integer,
    "fxAccountHolderId",
    "fxLedgerId",
    "fxClaimId",
    "Note",
    "fxEnteredById",
    "EnteredDate",
    "NoteTypeInd",
    "WarningNoteLevelInd",
    "ExpirationDate"
FROM raw."perm__Note";


-- ------------------------------------------------------------
-- VALIDATION: DISPLAY POPULATED ROW COUNTS
-- ------------------------------------------------------------

SELECT
    'permcontract' AS table_name,
    (SELECT COUNT(*) FROM raw."perm__Contract") AS raw_rows,
    (SELECT COUNT(*) FROM nesna.permcontract) AS nesna_rows

UNION ALL

SELECT
    'permcontractdisbursement',
    (SELECT COUNT(*) FROM raw."perm__ContractDisbursement"),
    (SELECT COUNT(*) FROM nesna.permcontractdisbursement)

UNION ALL

SELECT
    'permledger',
    (SELECT COUNT(*) FROM raw."perm__Ledger"),
    (SELECT COUNT(*) FROM nesna.permledger)

UNION ALL

SELECT
    'permnote',
    (SELECT COUNT(*) FROM raw."perm__Note"),
    (SELECT COUNT(*) FROM nesna.permnote);


-- ------------------------------------------------------------
-- COMMIT
-- ------------------------------------------------------------

COMMIT;