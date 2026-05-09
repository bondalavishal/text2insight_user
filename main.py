import os
import re
import time
import threading
import concurrent.futures
from datetime import datetime

_ts = lambda: datetime.now().strftime("%H:%M:%S")

from dotenv import load_dotenv
from flask import Flask as _Flask
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from app.llm.intent import classify_intent
from app.sql.guardrails import validate_sql, enforce_limit
from app.sql.connector import run_query
from app.slack.handler import (
    _check_unanswerable,
    _generate_sql_with_overrides,
    _split_questions,
    detect_anomalies,
    summarise_results,
    is_download_request,
    is_explain_request,
    generate_explanation,
    results_to_csv_string,
    STATS_PATTERN,
    DOWNLOAD_FOOTER,
    get_stats,
    cache_stats,
    get_cached,
    save_to_cache,
    log,
)
from app.eval.cache import update_cache_log_id
from app.rag.retriever import learn_pattern
from app.eval.interaction_logger import (
    get_user_info,
    log_interaction,
    mark_csv_downloaded,
    update_success_signal,
    evict_and_mark_negative,
    csv_string_to_bytes,
    results_to_json_string,
)
from app.llm.spellcheck import correct_prompt

load_dotenv()
app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# ── In-memory store: last results per user ────────────────────────────────────
# { user_id: { "results": [...], "csv_string": "...", "log_id": 123, "question": "..." } }
_last_interaction: dict = {}


# ── Progress bar ──────────────────────────────────────────────────────────────
def _progress_bar(pct: int, label: str) -> str:
    filled = int(pct / 10)
    bar    = "▓" * filled + "░" * (10 - filled)
    return f"⏳ *text2insight is thinking...*\n`{bar}` {pct}% — {label}"


# ── Single question pipeline ──────────────────────────────────────────────────
def _answer_with_progress(
    client, channel: str, ts: str, question: str, idx: int = None
) -> dict:
    """
    Runs the full SQL pipeline for a single question with live Slack progress updates.

    Returns a dict with keys:
      reply          : str   — text to post in Slack
      results        : list  — raw Databricks rows
      csv_string     : str   — CSV of results
      result_json    : str   — JSON string of results (for Databricks logging)
      status         : str   — success | cache_hit | blocked | disallowed_source | failed
      sql            : str   — generated SQL (empty if not reached)
      failure_reason : str   — populated on non-success statuses
      latency_ms     : int   — end-to-end latency in milliseconds
      cached         : bool  — True if served from ChromaDB cache
      anomaly_count  : int   — number of anomaly flags triggered
      rows_returned  : int   — number of Databricks rows
    """
    prefix = f"*{idx}.* " if idx is not None else ""
    start  = time.time()
    result = dict(
        reply="", results=[], csv_string="", result_json=None,
        status="failed", sql="", failure_reason="",
        latency_ms=0, cached=False, anomaly_count=0, rows_returned=0,
        similarity_matched_id=None,
        similarity_score=None,
    )

    # ── Cache check — instant, no progress bar ────────────────────────────────
    cached_entry = get_cached(question)
    if cached_entry:
        latency_ms = int((time.time() - start) * 1000)

        # Re-run anomaly detection using stored result_json so flags are
        # always fresh — even for entries cached before flags were stored.
        cached_answer = cached_entry["answer"]
        anomaly_count = 0
        try:
            import json as _json
            raw_json = cached_entry.get("result_json", "")
            if raw_json:
                cached_results = _json.loads(raw_json)
                flags = detect_anomalies(question, cached_results)
                if flags:
                    cached_answer = cached_answer + "\n" + "\n".join(flags)
                    anomaly_count = len(flags)
        except Exception:
            pass

        log(question=question, sql=cached_entry["sql"], rows_returned=0,
            latency_ms=latency_ms, cached=True, status="cache_hit")
        result.update(
            reply=f"{prefix}{cached_answer}{DOWNLOAD_FOOTER}",
            csv_string=cached_entry.get("csv_string", ""),
            status="cache_hit",
            sql=cached_entry["sql"],
            latency_ms=latency_ms,
            cached=True,
            anomaly_count=anomaly_count,
            similarity_matched_id=cached_entry.get("similarity_matched_id"),
            similarity_score=cached_entry.get("similarity"),
        )
        return result

    # ── Pre-flight unanswerable check ─────────────────────────────────────────
    reason = _check_unanswerable(question)
    if reason:
        latency_ms = int((time.time() - start) * 1000)
        log(question=question, latency_ms=latency_ms,
            status="blocked", failure_reason=reason)
        result.update(
            reply=f"{prefix}Sorry, that can't be answered: {reason}",
            status="blocked",
            failure_reason=reason,
            latency_ms=latency_ms,
        )
        return result

    # ── Generate SQL (20%) ────────────────────────────────────────────────────
    client.chat_update(channel=channel, ts=ts,
                       text=_progress_bar(20, "Generating SQL"))
    sql = _generate_sql_with_overrides(question)
    print(f"{_ts()} [text2insight] SQL: {sql}")
    result["sql"] = sql

    # ── Guardrails (40%) ──────────────────────────────────────────────────────
    client.chat_update(channel=channel, ts=ts,
                       text=_progress_bar(40, "Validating query"))
    is_valid, reason, failure_type = validate_sql(sql)
    if not is_valid:
        latency_ms = int((time.time() - start) * 1000)
        status_map = {
            "blocked_keyword":   "blocked",
            "disallowed_source": "disallowed_source",
            "invalid_start":     "failed",
        }
        guard_status = status_map.get(failure_type, "failed")
        log(question=question, sql=sql, latency_ms=latency_ms,
            status=guard_status, failure_reason=reason)
        result.update(
            reply=f"{prefix}Couldn't generate a safe query — try rephrasing.",
            status=guard_status,
            failure_reason=reason,
            latency_ms=latency_ms,
        )
        return result

    sql = enforce_limit(sql)
    result["sql"] = sql

    # ── Databricks execution (60%) ────────────────────────────────────────────
    client.chat_update(channel=channel, ts=ts,
                       text=_progress_bar(60, "Querying Databricks"))
    try:
        results = run_query(sql)
        print(f"{_ts()} [text2insight] Rows: {len(results)}")
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        log(question=question, sql=sql, latency_ms=latency_ms,
            status="failed", failure_reason=str(e))
        result.update(
            reply=f"{prefix}Query error — try rephrasing.",
            status="failed",
            failure_reason=str(e),
            latency_ms=latency_ms,
        )
        return result

    # ── Anomaly detection (80%) ───────────────────────────────────────────────
    client.chat_update(channel=channel, ts=ts,
                       text=_progress_bar(80, "Detecting anomalies"))
    flags = detect_anomalies(question, results)

    # ── Summarise (90%) ───────────────────────────────────────────────────────
    client.chat_update(channel=channel, ts=ts,
                       text=_progress_bar(90, "Summarising results"))
    summary = summarise_results(question, results)

    # ── Build reply ───────────────────────────────────────────────────────────
    reply = f"{prefix}{summary}"
    if flags:
        reply += "\n" + "\n".join(flags)
    reply += DOWNLOAD_FOOTER

    csv_string  = results_to_csv_string(results)
    result_json = results_to_json_string(results)
    latency_ms  = int((time.time() - start) * 1000)

    save_to_cache(question, summary, sql, csv_string, result_json)
    log(question=question, sql=sql, rows_returned=len(results),
        latency_ms=latency_ms, cached=False, status="success",
        anomaly_count=len(flags))

    result.update(
        reply=reply,
        results=results,
        csv_string=csv_string,
        result_json=result_json,
        status="success",
        latency_ms=latency_ms,
        cached=False,
        anomaly_count=len(flags),
        rows_returned=len(results),
    )
    return result


# ── Feedback handler ──────────────────────────────────────────────────────────
def _handle_feedback(client, user: str, channel: str, signal: str, log_id: int, question: str):
    """
    Processes a confirmed positive or negative signal.
    - Writes success_signal to Databricks
    - On negative: evicts the bad answer from ChromaDB + marks self_learned=FALSE
    - Acknowledges the user in Slack
    """
    update_success_signal(log_id, signal)

    if signal == "negative":
        evict_and_mark_negative(log_id, question)
        client.chat_postMessage(
            channel=channel,
            text=(
                f"<@{user}> Got it — I'll drop that answer from my memory. "
                f"Try asking again and I'll generate a fresh response."
            )
        )
        print(f"{_ts()} [Feedback] Negative signal processed: log_id={log_id} question={question[:60]}")
    else:
        client.chat_postMessage(
            channel=channel,
            text=f"<@{user}> Thanks for the feedback! 👍"
        )
        print(f"{_ts()} [Feedback] Positive signal recorded: log_id={log_id}")


# ── Core message handler ──────────────────────────────────────────────────────
def process_message(client, user: str, text: str, channel: str):
    print(f"\n{_ts()} [text2insight] User={user} Text={text}")
    raw_prompt = text  # preserve original for logging

    # ── Spellcheck — correct typos/shorthand before anything else ─────────────
    spellcheck_applied = False
    text = correct_prompt(text)
    if text != raw_prompt:
        spellcheck_applied = True
        print(f"{_ts()} [text2insight] Spellcheck applied: '{raw_prompt}' → '{text}'")

    last = _last_interaction.get(user)

    # ── Download request ──────────────────────────────────────────────────────
    if is_download_request(text):
        if not last:
            client.chat_postMessage(
                channel=channel,
                text=(
                    f"<@{user}> The download feature is available after you ask a data question. "
                    f"Go ahead and ask me something — once I answer, reply with *download* to get the results as a CSV!"
                )
            )
            return

        # Grab and clear before upload so concurrent duplicate events can't both proceed
        csv_files = last.get("csv_files", [])
        _last_interaction[user]["csv_files"] = []
        _last_interaction[user]["csv_string"] = ""  # backward-compat clear
        if not csv_files:
            client.chat_postMessage(
                channel=channel,
                text=(
                    f"<@{user}> Looks like you've already downloaded that one! "
                    f"Ask me another question — once I answer, reply with *download* to get the fresh results as a CSV."
                )
            )
            return

        failed = 0
        for i, cf in enumerate(csv_files, 1):
            slug     = re.sub(r'[^a-z0-9]+', '_', cf["question"].lower()[:30]).strip('_')
            filename = f"text2insight_q{i}_{slug}.csv" if len(csv_files) > 1 else "text2insight_data.csv"
            try:
                client.files_upload_v2(
                    channel=channel,
                    content=csv_string_to_bytes(cf["csv_string"]),
                    filename=filename,
                    title=f"Q{i}: {cf['question'][:60]}" if len(csv_files) > 1 else "text2insight Data Export",
                )
                print(f"{_ts()} [text2insight] CSV {i}/{len(csv_files)} uploaded for user={user}")
                if cf.get("log_id"):
                    mark_csv_downloaded(cf["log_id"])
            except Exception as e:
                failed += 1
                print(f"{_ts()} [text2insight] CSV {i} upload failed: {e}")

        if failed:
            client.chat_postMessage(
                channel=channel,
                text=f"<@{user}> {failed} file(s) couldn't be uploaded — try again."
            )

        user_info = get_user_info(client, user)
        log_interaction(
            user_id=user, email_id=user_info.get("email_id", ""),
            full_name=user_info.get("full_name", ""),
            raw_prompt=raw_prompt, question_asked=text,
            question_answered=f"CSV downloaded for: {last.get('question', '')}",
            status="failed" if failed else "success",
            interaction_type="download",
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
        )
        return

    # ── Explain request ───────────────────────────────────────────────────────
    if is_explain_request(text):
        if not last or not last.get("results"):
            client.chat_postMessage(
                channel=channel,
                text=(
                    f"<@{user}> The explain feature works after you ask a data question. "
                    f"Ask me something first — then reply with *explain* for a detailed analysis."
                )
            )
            return

        question    = last.get("question", "")
        results     = last.get("results", [])
        print(f"{_ts()} [text2insight] Generating explanation for user={user} ({len(results)} rows)")

        ts_thinking = client.chat_postMessage(
            channel=channel,
            text=f"<@{user}> ⏳ *Analysing your data...*"
        )["ts"]

        explanation = generate_explanation(question, results)
        header      = f"🔍 *Detailed breakdown for:* _{question}_\n\n"

        full_explanation = f"{header}{explanation}"
        client.chat_update(
            channel=channel,
            ts=ts_thinking,
            text=f"<@{user}> {full_explanation}",
        )
        print(f"{_ts()} [text2insight] Explanation posted for user={user}")

        user_info = get_user_info(client, user)
        log_interaction(
            user_id=user, email_id=user_info.get("email_id", ""),
            full_name=user_info.get("full_name", ""),
            raw_prompt=raw_prompt, question_asked=question,
            question_answered=full_explanation,
            status="success", interaction_type="explain",
            rows_returned=len(results),
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
        )
        return

    # ── Stats command ─────────────────────────────────────────────────────────
    if STATS_PATTERN.search(text):
        stats       = get_stats()
        cache       = cache_stats()
        stats_reply = (
            f"<@{user}> 📊 *text2insight Performance*\n"
            f"• Total questions: {stats.get('total', 0)}\n"
            f"• Pass rate: {stats.get('pass_rate', 'N/A')}\n"
            f"• Cache hit rate: {stats.get('cache_hit_rate', 'N/A')}\n"
            f"• Avg latency: {stats.get('avg_latency_ms', 0)}ms\n"
            f"• Avg cache latency: {stats.get('avg_cache_latency_ms', 0)}ms\n"
            f"• Total anomalies flagged: {stats.get('total_anomalies', 0)}\n"
            f"• Questions cached: {cache.get('total_cached', 0)}"
        )
        client.chat_postMessage(channel=channel, text=stats_reply)

        user_info = get_user_info(client, user)
        log_interaction(
            user_id=user, email_id=user_info.get("email_id", ""),
            full_name=user_info.get("full_name", ""),
            raw_prompt=raw_prompt, question_asked=text,
            question_answered=stats_reply,
            status="success", interaction_type="stats",
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
        )
        return

    # ── Intent + feedback classification (single LLM call) ───────────────────
    intent = classify_intent(text)
    print(f"{_ts()} [text2insight] Intent: {intent}")

    # Feedback intents — only act on them if there is a prior interaction to reference
    last = _last_interaction.get(user)
    if intent in ("feedback_positive", "feedback_negative"):
        if last and last.get("log_id") and last.get("question"):
            signal = "positive" if intent == "feedback_positive" else "negative"
            _handle_feedback(
                client, user, channel,
                signal=signal,
                log_id=last["log_id"],
                question=last["question"],
            )
            user_info = get_user_info(client, user)
            log_interaction(
                user_id=user, email_id=user_info.get("email_id", ""),
                full_name=user_info.get("full_name", ""),
                raw_prompt=raw_prompt, question_asked=last["question"],
                question_answered=f"Feedback signal: {signal}",
                status="success", interaction_type=f"feedback_{signal}",
                spellcheck_applied=spellcheck_applied,
                corrected_prompt=text if spellcheck_applied else None,
            )
            return
        # No prior interaction — fall through and treat as a data question

    if intent == "greeting":
        greeting_reply = (
            f"Hi <@{user}>! 👋 I'm text2insight — ask me anything about "
            f"orders, revenue, sellers, products or delivery performance.\n\n"
            f"You can ask multiple questions at once — "
            f"just number them or put each on a new line!"
        )
        client.chat_postMessage(channel=channel, text=greeting_reply)

        user_info = get_user_info(client, user)
        log_interaction(
            user_id=user, email_id=user_info.get("email_id", ""),
            full_name=user_info.get("full_name", ""),
            raw_prompt=raw_prompt, question_asked=text,
            question_answered=greeting_reply,
            status="success", interaction_type="greeting",
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
        )
        return

    if intent == "out_of_scope":
        oos_reply = (
            f"Sorry <@{user}>, I can only answer questions about "
            f"business data — orders, revenue, sellers, products, delivery."
        )
        client.chat_postMessage(channel=channel, text=oos_reply)

        user_info = get_user_info(client, user)
        log_interaction(
            user_id=user, email_id=user_info.get("email_id", ""),
            full_name=user_info.get("full_name", ""),
            raw_prompt=raw_prompt, question_asked=text,
            question_answered=oos_reply,
            status="success", interaction_type="out_of_scope",
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
        )
        return

    # ── Fetch user info for logging ───────────────────────────────────────────
    user_info = get_user_info(client, user)
    email     = user_info.get("email_id", "")
    full_name = user_info.get("full_name", "")

    # ── Split into individual questions ───────────────────────────────────────
    questions = _split_questions(text)
    MAX_Q     = 5
    questions = questions[:MAX_Q]

    if len(questions) == 1:
        # ── Single question ───────────────────────────────────────────────────
        msg = client.chat_postMessage(
            channel=channel,
            text=_progress_bar(10, "Understanding your question")
        )
        ts = msg["ts"]

        r = _answer_with_progress(client, channel, ts, questions[0])

        log_id = log_interaction(
            user_id=user, email_id=email, full_name=full_name,
            raw_prompt=raw_prompt,
            question_asked=questions[0],
            question_answered=r["reply"],
            status=r["status"],
            interaction_type="data_query",
            generated_sql=r["sql"] or None,
            result_json=r["result_json"],
            generated_csv=r["csv_string"] or None,
            failure_reason=r["failure_reason"] or None,
            similarity_matched_id=r["similarity_matched_id"],
            similarity_score=r["similarity_score"],
            self_learned=r["status"] == "success",
            latency_ms=r["latency_ms"],
            rows_returned=r["rows_returned"],
            anomaly_count=r["anomaly_count"],
            cached=r["cached"],
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
        )
        if r["status"] == "success" and log_id:
            update_cache_log_id(questions[0], log_id)
            if not r["cached"] and r["sql"]:
                learn_pattern(questions[0], r["sql"], log_id)

        _last_interaction[user] = {
            "results":    r["results"],
            "csv_string": r["csv_string"],
            "log_id":     log_id,
            "question":   questions[0],
            "csv_files":  [{"csv_string": r["csv_string"], "question": questions[0], "log_id": log_id}]
                          if r["csv_string"] else [],
        }

        client.chat_update(
            channel=channel,
            ts=ts,
            text=f"<@{user}> {r['reply']}"
        )

    else:
        # ── Multi-question: parallel execution (one progress bar per question) ─
        print(f"{_ts()} [text2insight] Multi-question: {len(questions)} questions (parallel)")

        # Post one progress bar message per question (slight stagger avoids burst)
        bar_ts: list[str] = []
        for i, q in enumerate(questions, 1):
            msg = client.chat_postMessage(
                channel=channel,
                text=_progress_bar(10, f"Q{i}: {q[:50]}..."),
            )
            bar_ts.append(msg["ts"])
            time.sleep(0.1)

        ordered_results: list[dict | None] = [None] * len(questions)
        last_results    = []
        last_csv_string = ""
        last_log_id     = None
        last_question   = ""

        future_to_idx: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for idx, (q, ts_i) in enumerate(zip(questions, bar_ts)):
                f = executor.submit(_answer_with_progress, client, channel, ts_i, q, idx + 1)
                future_to_idx[f] = idx

            # Update each message the moment its question finishes
            try:
                for future in concurrent.futures.as_completed(future_to_idx, timeout=200):
                    idx  = future_to_idx[future]
                    ts_i = bar_ts[idx]
                    try:
                        r = future.result()
                    except Exception as exc:
                        r = dict(
                            reply=f"*{idx+1}.* ❌ Error: {str(exc)[:80]}",
                            results=[], csv_string="", result_json=None,
                            status="failed", sql="", failure_reason=str(exc),
                            latency_ms=0, cached=False,
                            anomaly_count=0, rows_returned=0,
                            similarity_matched_id=None, similarity_score=None,
                        )
                    ordered_results[idx] = r
                    client.chat_update(
                        channel=channel, ts=ts_i,
                        text=f"<@{user}> {r['reply']}",
                    )
                    print(f"{_ts()} [text2insight] Q{idx+1} done ({r['status']})")

            except concurrent.futures.TimeoutError:
                print(f"{_ts()} [text2insight] Parallel timeout — filling remaining with error")
                for future, idx in future_to_idx.items():
                    if ordered_results[idx] is None:
                        ordered_results[idx] = dict(
                            reply=f"*{idx+1}.* ⏱ Timed out — try asking separately.",
                            results=[], csv_string="", result_json=None,
                            status="failed", sql="", failure_reason="timeout",
                            latency_ms=200000, cached=False,
                            anomaly_count=0, rows_returned=0,
                            similarity_matched_id=None, similarity_score=None,
                        )
                        client.chat_update(
                            channel=channel, ts=bar_ts[idx],
                            text=f"<@{user}> *{idx+1}.* ⏱ Timed out — try asking separately.",
                        )

        # Pre-set _last_interaction with log_id=None so concurrent download
        # requests don't see None while the slow logging loop is still running.
        # log_ids are backfilled into csv_files entries after each log_interaction call.
        csv_files: list[dict] = [
            {"csv_string": r["csv_string"], "question": questions[idx], "log_id": None}
            for idx, r in enumerate(ordered_results)
            if r and r.get("csv_string")
        ]
        _last_question_early  = next(
            (questions[idx] for idx in range(len(ordered_results) - 1, -1, -1)
             if ordered_results[idx] and ordered_results[idx].get("csv_string")), ""
        )
        _last_results_early   = next(
            (ordered_results[idx]["results"]
             for idx in range(len(ordered_results) - 1, -1, -1)
             if ordered_results[idx] and ordered_results[idx].get("results")), []
        )
        _last_interaction[user] = {
            "results":    _last_results_early,
            "csv_string": csv_files[-1]["csv_string"] if csv_files else "",
            "log_id":     None,
            "question":   _last_question_early,
            "csv_files":  csv_files,
        }

        # Log + cache in original question order; backfill log_ids into csv_files
        for idx, r in enumerate(ordered_results):
            if r is None:
                continue
            q = questions[idx]
            log_id = log_interaction(
                user_id=user, email_id=email, full_name=full_name,
                raw_prompt=raw_prompt,
                question_asked=q,
                question_answered=r["reply"],
                status=r["status"],
                interaction_type="data_query",
                generated_sql=r["sql"] or None,
                result_json=r["result_json"],
                generated_csv=r["csv_string"] or None,
                failure_reason=r["failure_reason"] or None,
                similarity_matched_id=r["similarity_matched_id"],
                similarity_score=r["similarity_score"],
                self_learned=r["status"] == "success",
                latency_ms=r["latency_ms"],
                rows_returned=r["rows_returned"],
                anomaly_count=r["anomaly_count"],
                cached=r["cached"],
                spellcheck_applied=spellcheck_applied,
                corrected_prompt=text if spellcheck_applied else None,
            )
            if r["status"] == "success" and log_id:
                update_cache_log_id(q, log_id)
                if not r["cached"] and r["sql"]:
                    learn_pattern(q, r["sql"], log_id)

            if r["csv_string"] and log_id:
                for cf in _last_interaction[user]["csv_files"]:
                    if cf["question"] == q and cf["log_id"] is None:
                        cf["log_id"] = log_id
                        break

            if r["results"] or r["csv_string"]:
                _last_interaction[user]["log_id"]  = log_id
                _last_interaction[user]["question"] = q


# ── Slack event handlers ──────────────────────────────────────────────────────
@app.message("")
def handle_message(message, client):
    user    = message.get("user", "unknown")
    text    = message.get("text", "").strip()
    channel = message.get("channel", "")
    if not text:
        return
    process_message(client, user, text, channel)


@app.event("app_mention")
def handle_mention(event, client):
    user    = event.get("user", "unknown")
    channel = event.get("channel", "")
    text    = " ".join(
        w for w in event.get("text", "").split()
        if not w.startswith("<@")
    ).strip()
    if not text:
        client.chat_postMessage(
            channel=channel,
            text=f"Hi <@{user}>! Ask me a question about the data."
        )
        return
    process_message(client, user, text, channel)


# ── Flask health check ────────────────────────────────────────────────────────
_health_app = _Flask(__name__)

@_health_app.route("/health")
def _health():
    return "ok", 200

def _run_health_server():
    port = int(os.getenv("FLASK_PORT", 3000))
    _health_app.run(host="0.0.0.0", port=port)


# ── Slack auto-reconnect ──────────────────────────────────────────────────────
def _run_slack():
    time.sleep(3)
    while True:
        try:
            print("text2insight connecting to Slack...")
            handler = SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN"))
            handler.start()
            print("Slack handler exited — reconnecting in 5s...")
        except Exception as e:
            print(f"Slack connection error: {e} — reconnecting in 5s...")
        time.sleep(5)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("text2insight starting...")
    threading.Thread(target=_run_health_server, daemon=True).start()
    print(f"Health check running on port {os.getenv('FLASK_PORT', 3000)}")
    threading.Thread(target=_run_slack, daemon=True).start()
    while True:
        time.sleep(60)
