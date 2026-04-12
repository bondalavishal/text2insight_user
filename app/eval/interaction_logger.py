"""
Interaction Logger — Phase 8 update.
Logs every InsightBot interaction to default.insightbot_interactions in Databricks.

New columns vs Phase 7:
  - log_id         : replaces index_id (BIGINT GENERATED ALWAYS AS IDENTITY)
  - status         : success | cache_hit | blocked | disallowed_source | failed
  - interaction_type: data_query | greeting | out_of_scope | stats | download
  - raw_prompt     : original Slack text before any splitting/cleaning
  - generated_sql  : SQL string sent to Databricks
  - result_json    : Databricks rows serialised as a JSON string
  - failure_reason : populated when status != success / cache_hit
  - alternative_suggestions : bot-suggested rephrases for failed queries (future)
  - similarity_matched_id   : log_id of the cache entry that was matched (future)
  - self_learned   : TRUE when this answer was added to the ChromaDB cache
  - success_signal : explicit user feedback e.g. thumbs-up reaction (future)
  - user_notified  : TRUE if an async follow-up message was sent (future)
  - latency_ms     : end-to-end latency in milliseconds
  - rows_returned  : number of Databricks rows returned
  - anomaly_count  : number of anomaly flags triggered
  - cached         : TRUE if answer was served from ChromaDB cache
"""

import csv
import io
import json
from app.sql.connector import run_query


def get_user_info(client, user_id: str) -> dict:
    try:
        response  = client.users_info(user=user_id)
        user      = response["user"]
        full_name = user.get("real_name") or user.get("name") or ""
        email_id  = user.get("profile", {}).get("email") or ""
        return {"full_name": full_name, "email_id": email_id}
    except Exception as e:
        print(f"[InteractionLogger] Could not fetch user info: {e}")
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
    self_learned:            bool  = False,
    success_signal:          str   = None,
    user_notified:           bool  = False,
    latency_ms:              int   = 0,
    rows_returned:           int   = 0,
    anomaly_count:           int   = 0,
    cached:                  bool  = False,
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
    INSERT INTO default.insightbot_interactions (
        ts, user_id, email_id, full_name,
        status, interaction_type,
        raw_prompt, question_asked, question_answered,
        generated_sql, result_json, generated_csv, csv_downloaded,
        failure_reason, alternative_suggestions,
        similarity_matched_id, self_learned,
        success_signal, user_notified,
        latency_ms, rows_returned, anomaly_count, cached
    ) VALUES (
        CURRENT_TIMESTAMP(),
        {sql_str(user_id)},
        {sql_str(email_id)},
        {sql_str(full_name)},
        {sql_str(status)},
        {sql_str(interaction_type)},
        {sql_str(raw_prompt)},
        {sql_str(question_asked)},
        {sql_str(question_answered)},
        {sql_str(generated_sql)},
        {sql_str(result_json)},
        {sql_str(generated_csv)},
        {sql_str(csv_downloaded)},
        {sql_str(failure_reason)},
        {sql_str(alternative_suggestions)},
        {sql_int(similarity_matched_id)},
        {sql_bool(self_learned)},
        {sql_str(success_signal)},
        {sql_bool(user_notified)},
        {sql_int(latency_ms)},
        {sql_int(rows_returned)},
        {sql_int(anomaly_count)},
        {sql_bool(cached)}
    )
    """
    try:
        run_query(insert_sql)
        result = run_query(f"""
            SELECT MAX(log_id) AS last_id
            FROM default.insightbot_interactions
            WHERE user_id = '{esc(user_id)}'
        """)
        if result:
            return result[0].get("last_id")
    except Exception as e:
        print(f"[InteractionLogger] Failed to log interaction: {e}")
    return None


def update_success_signal(log_id: int, signal: str) -> None:
    """
    Write 'positive' or 'negative' back to success_signal for a logged interaction.
    Called when the user reacts with an emoji or sends a short feedback reply.
    """
    if not log_id:
        return
    try:
        run_query(f"""
            UPDATE default.insightbot_interactions
            SET success_signal = '{signal}'
            WHERE log_id = {log_id}
        """)
        print(f"[InteractionLogger] success_signal='{signal}' set on log_id={log_id}")
    except Exception as e:
        print(f"[InteractionLogger] Failed to update success_signal: {e}")


def evict_and_mark_negative(log_id: int, question: str) -> None:
    """
    On a negative signal:
      1. Evicts the cached answer from ChromaDB so it won't be served again.
      2. Sets self_learned = FALSE in Databricks to reflect the eviction.

    This closes the feedback loop — a thumbs-down genuinely improves future answers.
    """
    from app.eval.cache import evict_from_cache

    evicted = evict_from_cache(question)
    if evicted:
        try:
            run_query(f"""
                UPDATE default.insightbot_interactions
                SET self_learned = FALSE
                WHERE log_id = {log_id}
            """)
            print(f"[InteractionLogger] Cache evicted + self_learned=FALSE for log_id={log_id}")
        except Exception as e:
            print(f"[InteractionLogger] Failed to update self_learned after eviction: {e}")


def mark_csv_downloaded(log_id: int) -> None:
    if not log_id:
        return
    try:
        run_query(f"""
            UPDATE default.insightbot_interactions
            SET csv_downloaded = 'yes'
            WHERE log_id = {log_id}
        """)
        print(f"[InteractionLogger] Marked log_id={log_id} as csv_downloaded=yes")
    except Exception as e:
        print(f"[InteractionLogger] Failed to update csv_downloaded: {e}")


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
    """Serialises Databricks rows to a JSON string for storage in result_json."""
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
        rows = run_query("""
            SELECT question_asked, question_answered, generated_sql
            FROM default.insightbot_interactions
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
