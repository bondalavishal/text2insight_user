-- default.text2insight_user_query_log
--
-- Unified interaction log and self-learning library.
-- Schema matches the existing interaction export (default.text2insight_user_query_log.csv).
-- embedding_id links each successful query to its ChromaDB vector for fast retrieval.
--
-- Run once in Databricks SQL editor.

CREATE TABLE IF NOT EXISTS default.text2insight_user_query_log (
    log_id                   BIGINT GENERATED ALWAYS AS IDENTITY,
    ts                       TIMESTAMP,
    user_id                  STRING,
    email_id                 STRING,
    full_name                STRING,
    raw_prompt               STRING,
    spellcheck_applied       BOOLEAN,
    corrected_prompt         STRING,
    status                   STRING,    -- success | cache_hit | blocked | disallowed_source | failed
    interaction_type         STRING,    -- data_query | greeting | out_of_scope | stats | download
    question_asked           STRING,
    question_answered        STRING,
    generated_sql            STRING,
    result_json              STRING,
    generated_csv            STRING,
    csv_downloaded           STRING,    -- 'yes' | 'no'
    failure_reason           STRING,
    alternative_suggestions  STRING,
    similarity_matched_id    BIGINT,
    similarity_score         DOUBLE,
    self_learned             BOOLEAN,
    success_signal           STRING,    -- NULL | 'positive' | 'negative'
    latency_ms               BIGINT,
    rows_returned            INT,
    anomaly_count            INT,
    cached                   BOOLEAN,
    embedding_id             STRING     -- ChromaDB document ID for self-learning linkage
);
