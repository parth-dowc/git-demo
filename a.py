
SET NOCOUNT ON;

-- ============================================================
-- NESNA SOURCE ROW COUNT VALIDATION
-- All 66 unique tables/views
-- Exact COUNT_BIG(*) values
-- Read-only: no permanent tables or data modifications
-- ============================================================

DECLARE @sources TABLE (
    source_schema SYSNAME NOT NULL,
    source_table SYSNAME NOT NULL,
    PRIMARY KEY (source_schema, source_table)
);

INSERT INTO @sources (source_schema, source_table)
VALUES

-- CLASS
('class', 'VINMaster'),

-- CONTRACT
('contract', 'ContractField'),

-- DIM
('dim', 'AccountHolder'),
('dim', 'Cancel'),
('dim', 'ContractEventType'),
('dim', 'ContractField'),
('dim', 'Customer'),

-- FACT
('fact', 'Contract'),
('fact', 'ContractField'),

-- INCR
('incr', 'Renewal'),

-- PERM
('perm', 'ClaimDetailComponentHistory'),
('perm', 'ClaimDetailComponentLaborHistory'),
('perm', 'ClaimDetailComponentMain'),
('perm', 'ClaimDetailComponentPartHistory'),
('perm', 'ClaimDetailComponentPartMain'),
('perm', 'ClaimDetailMain'),
('perm', 'ClaimHistory'),
('perm', 'ClaimMain'),
('perm', 'Contract'),
('perm', 'ContractDisbursement'),
('perm', 'ContractField'),
('perm', 'ContractPDF'),
('perm', 'Dealer'),
('perm', 'Field'),
('perm', 'Ledger'),
('perm', 'MembershipUser'),
('perm', 'Note'),
('perm', 'Plan'),
('perm', 'Product'),
('perm', 'RateBasedRule'),
('perm', 'RateBasedRuleGroup'),
('perm', 'Renewal'),

-- SSAS
('ssas', 'SSASAccountHolderDimension'),
('ssas', 'SSASAdminCompanyDimension'),
('ssas', 'SSASAgentDimension'),
('ssas', 'SSASAmendmentDimension'),
('ssas', 'SSASAmendmentTransaction'),
('ssas', 'SSASCancel'),
('ssas', 'SSASCancelAdjustmentCost'),
('ssas', 'SSASCancelDimension'),
('ssas', 'SSASCCARCAccountHolderDimension'),
('ssas', 'SSASCCARCCashTransactionDimension'),
('ssas', 'SSASClaimDimension'),
('ssas', 'SSASClaimHistoryChange'),
('ssas', 'SSASClaimPaymentChange'),
('ssas', 'SSASClaimPaymentTotal'),
('ssas', 'SSASComponentDimension'),
('ssas', 'SSASComponentHistoryChange'),
('ssas', 'SSASComponentHistoryTotal'),
('ssas', 'SSASContractDimension'),
('ssas', 'SSASContractFieldData'),
('ssas', 'SSASDealerDimension'),
('ssas', 'SSASDealerGroupDimension'),
('ssas', 'SSASDisbursementDimension'),
('ssas', 'SSASEarnedDisbursement'),
('ssas', 'SSASEarnedReserve'),
('ssas', 'SSASInsurerDimension'),
('ssas', 'SSASLienHolderDimension'),
('ssas', 'SSASPostingHistoryDimension'),
('ssas', 'SSASReinstatement'),
('ssas', 'SSASReinstatementDimension'),
('ssas', 'SSASReinsurerDimension'),
('ssas', 'SSASRepairFacilityDimension'),
('ssas', 'SSASTransaction'),

-- WORK
('work', 'SSASClaimHistoryChange_ByCategory'),
('work', 'SSASDisbursements');


-- ============================================================
-- RESULTS STORAGE
-- ============================================================

DECLARE @results TABLE (
    source_schema SYSNAME,
    source_table SYSNAME,
    source_rows BIGINT NULL,
    count_status VARCHAR(20),
    duration_seconds DECIMAL(18,3) NULL,
    counted_at_utc DATETIME2(3) NULL,
    error_message NVARCHAR(4000) NULL
);

DECLARE
    @schema SYSNAME,
    @table SYSNAME,
    @sql NVARCHAR(MAX),
    @count BIGINT,
    @started DATETIME2(3),
    @finished DATETIME2(3);


-- ============================================================
-- COUNT ALL SOURCES
-- ============================================================

DECLARE source_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT source_schema, source_table
    FROM @sources
    ORDER BY source_schema, source_table;

OPEN source_cursor;

FETCH NEXT FROM source_cursor
INTO @schema, @table;

WHILE @@FETCH_STATUS = 0
BEGIN
    SET @count = NULL;
    SET @started = SYSUTCDATETIME();

    RAISERROR(
        N'Counting %s.%s',
        0,
        1,
        @schema,
        @table
    ) WITH NOWAIT;

    BEGIN TRY

        SET @sql =
            N'SELECT @result = COUNT_BIG(*) FROM '
            + QUOTENAME(@schema)
            + N'.'
            + QUOTENAME(@table);

        EXEC sys.sp_executesql
            @sql,
            N'@result BIGINT OUTPUT',
            @result = @count OUTPUT;

        SET @finished = SYSUTCDATETIME();

        INSERT INTO @results
        VALUES (
            @schema,
            @table,
            @count,
            'success',
            DATEDIFF_BIG(
                MILLISECOND,
                @started,
                @finished
            ) / 1000.0,
            @finished,
            NULL
        );

    END TRY
    BEGIN CATCH

        SET @finished = SYSUTCDATETIME();

        INSERT INTO @results
        VALUES (
            @schema,
            @table,
            NULL,
            'failed',
            DATEDIFF_BIG(
                MILLISECOND,
                @started,
                @finished
            ) / 1000.0,
            @finished,
            ERROR_MESSAGE()
        );

    END CATCH;

    FETCH NEXT FROM source_cursor
    INTO @schema, @table;

END;

CLOSE source_cursor;
DEALLOCATE source_cursor;


-- ============================================================
-- RESULT 1: INDIVIDUAL SOURCE COUNTS
-- ============================================================

SELECT
    source_schema,
    source_table,
    source_rows,
    count_status,
    duration_seconds,
    counted_at_utc,
    error_message
FROM @results
ORDER BY
    source_schema,
    source_table;


-- ============================================================
-- RESULT 2: GRAND TOTAL AND VALIDATION SUMMARY
-- ============================================================

SELECT
    COUNT(*) AS total_tables,

    SUM(
        CASE
            WHEN count_status = 'success' THEN 1
            ELSE 0
        END
    ) AS successful_counts,

    SUM(
        CASE
            WHEN count_status = 'failed' THEN 1
            ELSE 0
        END
    ) AS failed_counts,

    SUM(source_rows) AS total_source_rows,

    CAST(
        SUM(source_rows) / 1000000.0
        AS DECIMAL(18,2)
    ) AS total_rows_millions,

    CAST(
        SUM(duration_seconds) / 60.0
        AS DECIMAL(18,2)
    ) AS total_count_duration_minutes

FROM @results;
