"""
Interaction Logger — logs every text2insight interaction to
default.text2insight_user_query_log in Databricks.

This table is the unified interaction log and self-learning library.
embedding_id links successful queries to their ChromaDB vector for RAG retrieval.
"""

import csv
import io
import json
from datetime import datetime
from app.sql.connector import run_query

_ts = lambda: datetime.now().strftime("%H:%M:%S")

TABLE = "default.text2insight_user_query_log"

# ── DDL: run once in Databricks for any columns that do not exist yet ───────────
# ALTER TABLE default.text2insight_user_query_log ADD COLUMN viz_spec_json STRING;
# ALTER TABLE default.text2insight_user_query_log ADD COLUMN explain_text   STRING;
# ────────────────────────────────────────────────────────────────────────────────


def get_user_info(client, user_id: str) -> dict:
    try:
        response  = client.users_info(user=user_id)
        user      = response["user"]
        full_name = user.get("real_name") or user.get("name") or ""
        email_id  = user.get("profile", {}).get("email") or ""
        return {"full_name": full_name, "email_id": email_id}
    except Exception as e:
        print(f"{_ts()} [InteractionLogger] Could not fetch user info: {e}")
        return {"full_name": "", "email_id": ""}


def log_interaction(
    user_id:                 str,
    email_id:                str,
    full_name:               str,
    raw_prompt:              str,
    question_asked:          str,
    question_answered:       str,
    status:                  str   = "success",
    interaction_type:        str   = None,
    generated_sql:           str   = None,
    result_json:             str   = None,
    generated_csv:           str   = None,
    csv_downloaded:          str   = "no",
    failure_reason:          str   = None,
    alternative_suggestions: str   = None,
    similarity_matched_id:   int   = None,
    similarity_score:        float = None,
    self_learned:            bool  = False,
    success_signal:          str   = None,
    latency_ms:              int   = 0,
    rows_returned:           int   = 0,
    anomaly_count:           int   = 0,
    cached:                  bool  = False,
    spellcheck_applied:      bool  = False,
    corrected_prompt:        str   = None,
    embedding_id:            str   = None,
    viz_spec_json:           str   = None,  # serialised VizSpec — enables chart regen on cache hit
    explain_text:            str   = None,  # structured business analysis — served on cache hits
) -> int | None:

    def esc(s):
        return (s or "").replace("'", "''")

    def sql_str(val):
        return f"'{esc(str(val))}'" if val is not None else "NULL"

    def sql_bool(val):
        return "TRUE" if val else "FALSE"

    def sql_int(val):
        return str(int(val)) if val is not None else "NULL"

    insert_sql = f"""
    INSERT INTO {TABLE} (
        ts, user_id, email_id, full_name,
        raw_prompt, spellcheck_applied, corrected_prompt,
        status, interaction_type,
        question_asked, question_answered,
        generated_sql, result_json, generated_csv, csv_downloaded,
        failure_reason, alternative_suggestions,
        similarity_matched_id, similarity_score, self_learned,
        success_signal,
        latency_ms, rows_returned, anomaly_count, cached,
        embedding_id, viz_spec_json, explain_text
    ) VALUES (
        CURRENT_TIMESTAMP(),
        {sql_str(user_id)},
        {sql_str(email_id)},
        {sql_str(full_name)},
        {sql_str(raw_prompt)},
        {sql_bool(spellcheck_applied)},
        {sql_str(corrected_prompt)},
        {sql_str(status)},
        {sql_str(interaction_type)},
        {sql_str(question_asked)},
        {sql_str(question_answered)},
        {sql_str(generated_sql)},
        {sql_str(result_json)},
        {sql_str(generated_csv)},
        {sql_str(csv_downloaded)},
        {sql_str(failure_reason)},
        {sql_str(alternative_suggestions)},
        {sql_int(similarity_matched_id)},
        {f"{float(similarity_score)}" if similarity_score is not None else "NULL"},
        {sql_bool(self_learned)},
        {sql_str(success_signal)},
        {sql_int(latency_ms)},
        {sql_int(rows_returned)},
        {sql_int(anomaly_count)},
        {sql_bool(cached)},
        {sql_str(embedding_id)},
        {sql_str(viz_spec_json)},
        {sql_str(explain_text)}
    )
    """
    try:
        run_query(insert_sql)
        result = run_query(f"""
            SELECT MAX(log_id) AS last_id
            FROM {TABLE}
            WHERE user_id = '{esc(user_id)}'
        """)
        if result:
            return result[0].get("last_id")
    except Exception as e:
        print(f"{_ts()} [InteractionLogger] Failed to log interaction: {e}")
    return None


def update_embedding_id(log_id: int, embedding_id: str) -> None:
    """Backfills the ChromaDB embedding_id after learn_pattern() completes."""
    if not log_id or not embedding_id:
        return
    try:
        run_query(f"""
            UPDATE {TABLE}
            SET embedding_id = '{embedding_id}'
            WHERE log_id = {log_id}
        """)
    except Exception as e:
        print(f"{_ts()} [InteractionLogger] Failed to update embedding_id: {e}")


def update_success_signal(log_id: int, signal: str) -> None:
    if not log_id:
        return
    try:
        run_query(f"""
            UPDATE {TABLE}
            SET success_signal = '{signal}'
            WHERE log_id = {log_id}
        """)
        print(f"{_ts()} [InteractionLogger] success_signal='{signal}' set on log_id={log_id}")
    except Exception as e:
        print(f"{_ts()} [InteractionLogger] Failed to update success_signal: {e}")


def evict_and_mark_negative(log_id: int, question: str) -> None:
    """
    On a negative signal: evicts the cached answer from ChromaDB and
    sets self_learned = FALSE in the log table.
    """
    from app.eval.cache import evict_from_cache

    evicted = evict_from_cache(question)
    if evicted:
        try:
            run_query(f"""
                UPDATE {TABLE}
                SET self_learned = FALSE
                WHERE log_id = {log_id}
            """)
            print(f"{_ts()} [InteractionLogger] Cache evicted + self_learned=FALSE for log_id={log_id}")
        except Exception as e:
            print(f"{_ts()} [InteractionLogger] Failed to update self_learned after eviction: {e}")


def mark_csv_downloaded(log_id: int) -> None:
    if not log_id:
        return
    try:
        run_query(f"""
            UPDATE {TABLE}
            SET csv_downloaded = 'yes'
            WHERE log_id = {log_id}
        """)
        print(f"{_ts()} [InteractionLogger] Marked log_id={log_id} as csv_downloaded=yes")
    except Exception as e:
        print(f"{_ts()} [InteractionLogger] Failed to update csv_downloaded: {e}")


def results_to_csv_string(results: list[dict]) -> str:
    if not results:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=results[0].keys())
    writer.writeheader()
    writer.writerows(results)
    return output.getvalue()


def csv_string_to_bytes(csv_string: str) -> bytes:
    return csv_string.encode("utf-8")


def results_to_json_string(results: list[dict]) -> str | None:
    if not results:
        return None
    try:
        return json.dumps(results, default=str)
    except Exception:
        return None


def seed_cache_from_log() -> int:
    from app.eval.cache import save_to_cache

    print("Re-seeding ChromaDB cache from Databricks interaction log...")
    try:
        rows = run_query(f"""
            SELECT question_asked, question_answered, generated_sql
            FROM {TABLE}
            WHERE question_answered IS NOT NULL
              AND question_answered != ''
              AND status IN ('success', 'cache_hit')
            ORDER BY ts ASC
        """)
        if not rows:
            print("  No rows found in log table. Cache not seeded.")
            return 0
        count = 0
        for row in rows:
            q   = row.get("question_asked", "")
            a   = row.get("question_answered", "")
            sql = row.get("generated_sql", "") or ""
            if q and a:
                save_to_cache(question=q, answer=a, sql=sql)
                count += 1
        print(f"  Seeded {count} Q&A pairs into ChromaDB cache.")
        return count
    except Exception as e:
        print(f"  Failed to seed cache: {e}")
        return 0


if __name__ == "__main__":
    seed_cache_from_log()
