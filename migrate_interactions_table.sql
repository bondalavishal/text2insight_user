-- =============================================================================
-- InsightBot — Phase 8 Table Migration
-- Run this in your Databricks SQL editor against the default schema.
-- =============================================================================

-- Step 1: Add all new columns to the existing table.
-- Existing rows will have NULL for every new column, which is safe.

ALTER TABLE default.insightbot_interactions
  ADD COLUMNS (
    status                   STRING,
    interaction_type         STRING,
    raw_prompt               STRING,
    generated_sql            STRING,
    result_json              STRING,
    failure_reason           STRING,
    alternative_suggestions  STRING,
    similarity_matched_id    BIGINT,
    self_learned             BOOLEAN,
    success_signal           STRING,
    user_notified            BOOLEAN,
    latency_ms               BIGINT,
    rows_returned            INT,
    anomaly_count            INT,
    cached                   BOOLEAN
  );


-- Step 2: Rename index_id → log_id.
-- Requires Databricks Runtime 10.4+ with Delta Lake.
-- If your runtime doesn't support RENAME COLUMN, skip this step
-- and update your app config to use index_id instead.

ALTER TABLE default.insightbot_interactions
  RENAME COLUMN index_id TO log_id;


-- Step 3: Backfill status for all legacy rows so existing analytics
-- queries don't break on NULL status values.

UPDATE default.insightbot_interactions
SET status = 'success'
WHERE status IS NULL;


-- Step 4: Verify the final schema.

DESCRIBE TABLE default.insightbot_interactions;

-- Expected columns (order may vary):
--   log_id                   BIGINT (was index_id)
--   ts                       TIMESTAMP
--   user_id                  STRING
--   email_id                 STRING
--   full_name                STRING
--   status                   STRING
--   interaction_type         STRING
--   raw_prompt               STRING
--   question_asked           STRING
--   question_answered        STRING
--   generated_sql            STRING
--   result_json              STRING
--   generated_csv            STRING
--   csv_downloaded           STRING
--   failure_reason           STRING
--   alternative_suggestions  STRING
--   similarity_matched_id    BIGINT
--   self_learned             BOOLEAN
--   success_signal           STRING
--   user_notified            BOOLEAN
--   latency_ms               BIGINT
--   rows_returned            INT
--   anomaly_count            INT
--   cached                   BOOLEAN
