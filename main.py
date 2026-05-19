import os
import re
import json
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
    summarise_and_explain,
    generate_explanation,   # used for cache-hit explain backfill only
    # is_download_request, is_explain_request — kept for re-enable with manual handlers
    is_download_request,
    is_explain_request,
    results_to_csv_string,
    STATS_PATTERN,
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
    # mark_csv_downloaded — kept for re-enable with download handler
    update_success_signal,
    evict_and_mark_negative,
    csv_string_to_bytes,
    results_to_json_string,
)
from app.llm.spellcheck import correct_prompt
from app.slack.chart_generator import classify_viz, generate_chart, VizSpec

load_dotenv()
app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# ── In-memory store: last results per user ────────────────────────────────────
# { user_id: { "results": [...], "csv_string": "...", "log_id": 123, "question": "..." } }
_last_interaction: dict = {}
_last_interaction_lock = threading.Lock()


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
        chart_bytes=None,
        viz_spec_json=None,    # serialised VizSpec — stored in cache for chart regen
        explain_text=None,     # structured business analysis — stored in cache + Databricks
    )

    # ── Cache check — instant, no progress bar ────────────────────────────────
    cached_entry = get_cached(question)
    if cached_entry:
        latency_ms = int((time.time() - start) * 1000)

        # Re-run anomaly detection using stored result_json so flags are
        # always fresh — even for entries cached before flags were stored.
        # Also regenerate the chart from stored viz_spec_json + result_json
        # so the image is rebuilt in ~40 ms with no LLM call required.
        cached_answer  = cached_entry["answer"]
        anomaly_count  = 0
        chart_bytes    = None
        cached_results = []
        try:
            raw_json = cached_entry.get("result_json", "")
            if raw_json:
                cached_results = json.loads(raw_json)
                flags = detect_anomalies(question, cached_results)
                if flags:
                    cached_answer = cached_answer + "\n" + "\n".join(flags)
                    anomaly_count = len(flags)

                # Chart regen from cached VizSpec (no LLM needed)
                vsp_json = cached_entry.get("viz_spec_json", "")
                if vsp_json:
                    try:
                        spec = VizSpec(**json.loads(vsp_json))
                        chart_bytes = generate_chart(question, cached_results, spec, use_codegen=False)
                        if chart_bytes:
                            print(f"{_ts()} [Chart] Cache-hit regen OK ({len(chart_bytes)//1024}KB)")
                    except Exception as _ce:
                        print(f"{_ts()} [Chart] Cache-hit regen failed: {_ce}")
        except Exception:
            pass

        # ── If result_json is absent, re-run the cached SQL to get raw data ───
        # Without rows we can't regenerate CSV / chart / explain — so always
        # ensure we have data before attempting to fill missing deliverables.
        result_json_c = cached_entry.get("result_json", "") or ""
        if not cached_results and cached_entry.get("sql"):
            try:
                cached_results = run_query(cached_entry["sql"])
                if cached_results:
                    result_json_c = results_to_json_string(cached_results) or ""
                    flags_rerun   = detect_anomalies(question, cached_results)
                    if flags_rerun:
                        cached_answer  = cached_answer + "\n" + "\n".join(flags_rerun)
                        anomaly_count  = len(flags_rerun)
                    print(f"{_ts()} [Cache] Re-ran SQL for missing result_json ({len(cached_results)} rows)")
            except Exception as _qe:
                print(f"{_ts()} [Cache] SQL re-run failed: {_qe}")

        # ── Fill missing deliverables (csv / chart / explain) ─────────────────
        # Regenerate any that are absent, then backfill into cache so future
        # hits get everything instantly without re-running SQL or the LLM.
        csv_string_c    = cached_entry.get("csv_string", "") or ""
        explain_text_c  = cached_entry.get("explain_text", "") or ""
        _new_vsp_json   = None
        _needs_backfill = bool(result_json_c and not (cached_entry.get("result_json") or ""))

        if cached_results:
            if not csv_string_c:
                csv_string_c    = results_to_csv_string(cached_results)
                _needs_backfill = True

            if not chart_bytes:
                try:
                    spec = classify_viz(question, cached_results)
                    if spec:
                        chart_bytes   = generate_chart(question, cached_results, spec, use_codegen=False)
                        _new_vsp_json = json.dumps(
                            {k: v for k, v in spec.__dict__.items()}, default=str
                        )
                        _needs_backfill = True
                        if chart_bytes:
                            print(f"{_ts()} [Chart] Cache-hit classify regen OK ({len(chart_bytes)//1024}KB)")
                except Exception as _ce:
                    print(f"{_ts()} [Chart] Cache-hit classify regen failed: {_ce}")

            if not explain_text_c:
                try:
                    explain_text_c = generate_explanation(question, cached_results) or ""
                    if explain_text_c:
                        _needs_backfill = True
                        print(f"{_ts()} [Cache] Regenerated explain_text for: {question[:60]}...")
                except Exception as _ee:
                    print(f"{_ts()} [Cache] explain_text regen failed: {_ee}")

            if _needs_backfill:
                save_to_cache(
                    question,
                    cached_entry["answer"],
                    cached_entry["sql"],
                    csv_string_c,
                    result_json_c,
                    _new_vsp_json or cached_entry.get("viz_spec_json", ""),
                    explain_text_c,
                )

        log(question=question, sql=cached_entry["sql"], rows_returned=0,
            latency_ms=latency_ms, cached=True, status="cache_hit")
        result.update(
            reply=f"{prefix}{cached_answer}",
            results=cached_results,
            result_json=result_json_c,
            csv_string=csv_string_c,
            status="cache_hit",
            sql=cached_entry["sql"],
            latency_ms=latency_ms,
            cached=True,
            anomaly_count=anomaly_count,
            chart_bytes=chart_bytes,
            similarity_matched_id=cached_entry.get("similarity_matched_id"),
            similarity_score=cached_entry.get("similarity"),
            explain_text=explain_text_c,
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
        print(f"{_ts()} [Guardrail] FAILED ({failure_type}): {reason}")
        print(f"{_ts()} [Guardrail] Rejected SQL:\n{sql}")
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

    # ── LLM "cannot answer" sentinel — return before guardrails ──────────────
    if re.search(r"cannot be answered", sql, re.IGNORECASE):
        latency_ms = int((time.time() - start) * 1000)
        log(question=question, sql=sql, latency_ms=latency_ms,
            status="blocked", failure_reason="unanswerable")
        result.update(
            reply=f"{prefix}I don't have the data to answer that — try rephrasing or ask something about orders, revenue, sellers, products or delivery.",
            status="blocked",
            failure_reason="unanswerable",
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

    # ── Summary + Explain in one LLM call (90%) ──────────────────────────────
    client.chat_update(channel=channel, ts=ts,
                       text=_progress_bar(90, "Analysing results"))
    summary, explain_text = summarise_and_explain(question, results)

    # ── Build reply (summary + anomaly flags; explain posted separately) ──────
    reply = f"{prefix}{summary}"
    if flags:
        reply += "\n" + "\n".join(flags)

    csv_string  = results_to_csv_string(results)
    result_json = results_to_json_string(results)
    latency_ms  = int((time.time() - start) * 1000)

    # ── Chart generation (silent fail — never blocks the response) ────────────
    chart_bytes   = None
    viz_spec_json = None
    try:
        spec = classify_viz(question, results)
        if spec:
            chart_bytes = generate_chart(question, results, spec)
            # Serialise spec for cache — enables chart regen on future cache hits
            viz_spec_json = json.dumps(
                {k: v for k, v in spec.__dict__.items()}, default=str
            )
            if chart_bytes:
                print(f"{_ts()} [Chart] Generated {spec.chart_type} chart ({len(chart_bytes)//1024}KB)")
    except Exception as _ce:
        print(f"{_ts()} [Chart] Skipped: {_ce}")

    save_to_cache(question, summary, sql, csv_string, result_json,
                  viz_spec_json or "", explain_text or "")
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
        chart_bytes=chart_bytes,
        viz_spec_json=viz_spec_json,
        explain_text=explain_text,
    )
    return result


# ── One-shot delivery helper ──────────────────────────────────────────────────
def _deliver(
    client, user: str, channel: str, question: str, r: dict,
    ts: str, q_label: str = ""
) -> None:
    """
    Replaces the Slack progress-bar placeholder with the result, then auto-uploads
    CSV and chart, and posts the explain analysis — all in one shot.

    q_label: optional label like "Q1", "Q2" used in filenames/headers.
    """
    # 1. Replace progress bar with summary
    client.chat_update(channel=channel, ts=ts, text=f"<@{user}> {r['reply']}")

    # 2. CSV auto-upload
    if r.get("csv_string"):
        try:
            slug  = re.sub(r'[^a-z0-9]+', '_', question.lower()[:30]).strip('_')
            fname = f"text2insight_{q_label+'_' if q_label else ''}{slug}.csv"
            client.files_upload_v2(
                channel=channel,
                content=csv_string_to_bytes(r["csv_string"]),
                filename=fname,
                title=question[:70],
            )
        except Exception as _e:
            print(f"{_ts()} [text2insight] {q_label+' ' if q_label else ''}CSV upload failed: {_e}")

    # 3. Chart upload
    if r.get("chart_bytes"):
        try:
            client.files_upload_v2(
                channel=channel,
                content=r["chart_bytes"],
                filename=f"insight{'_'+q_label.lower() if q_label else ''}.png",
                title=question[:70],
            )
        except Exception as _e:
            print(f"{_ts()} [Chart] {q_label+' ' if q_label else ''}upload failed: {_e}")
    elif r.get("status") in ("success", "cache_hit"):
        try:
            client.chat_postMessage(
                channel=channel,
                text=f"<@{user}> _Couldn't render a chart for this query._",
            )
        except Exception:
            pass

    # 4. Explain post
    if r.get("explain_text"):
        try:
            hdr = (
                f"🔍 *{q_label+' ' if q_label else ''}Detailed breakdown:*"
                f" _{question}_\n\n"
            )
            client.chat_postMessage(
                channel=channel,
                text=f"<@{user}> {hdr}{r['explain_text']}"
            )
        except Exception as _e:
            print(f"{_ts()} [text2insight] {q_label+' ' if q_label else ''}explain post failed: {_e}")


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

    with _last_interaction_lock:
        last = _last_interaction.get(user)

    # ── Pending-batch number selection ────────────────────────────────────────
    # When all questions in a multi-question batch hit the cache, Q1 is answered
    # fully and the user is offered Q2-QN via a numbered menu.  This handler
    # fires when they reply with one of those numbers.
    if last and last.get("pending_batch") and re.fullmatch(r'\d+', text.strip()):
        choice  = int(text.strip())
        pending = last["pending_batch"]   # list of {"question": str} for Q2-QN
        if 2 <= choice <= len(pending) + 1:
            batch_q = pending[choice - 2]["question"]
            u_info  = get_user_info(client, user)
            u_email = u_info.get("email_id", "")
            u_name  = u_info.get("full_name", "")

            msg   = client.chat_postMessage(
                channel=channel,
                text=_progress_bar(10, f"Loading answer {choice}...")
            )
            ts_b = msg["ts"]
            r_b  = _answer_with_progress(client, channel, ts_b, batch_q)

            b_log_id = log_interaction(
                user_id=user, email_id=u_email, full_name=u_name,
                raw_prompt=raw_prompt, question_asked=batch_q,
                question_answered=r_b["reply"],
                status=r_b["status"], interaction_type="data_query",
                generated_sql=r_b["sql"] or None,
                result_json=r_b.get("result_json"),
                generated_csv=r_b["csv_string"] or None,
                failure_reason=r_b.get("failure_reason") or None,
                similarity_matched_id=r_b.get("similarity_matched_id"),
                similarity_score=r_b.get("similarity_score"),
                self_learned=r_b["status"] == "success",
                latency_ms=r_b["latency_ms"],
                rows_returned=r_b.get("rows_returned", 0),
                anomaly_count=r_b.get("anomaly_count", 0),
                cached=r_b["cached"],
                csv_downloaded="yes" if r_b.get("csv_string") else "no",
                spellcheck_applied=spellcheck_applied,
                corrected_prompt=text if spellcheck_applied else None,
                viz_spec_json=r_b.get("viz_spec_json"),
                explain_text=r_b.get("explain_text"),
            )
            if r_b["status"] == "cache_hit" and b_log_id:
                update_cache_log_id(batch_q, b_log_id)

            _deliver(client, user, channel, batch_q, r_b, ts_b, q_label=f"Q{choice}")

            # Preserve pending_batch so other numbers can still be requested
            with _last_interaction_lock:
                _last_interaction[user] = {
                    **last,
                    "results":    r_b.get("results", []),
                    "csv_string": r_b["csv_string"],
                    "log_id":     b_log_id,
                    "question":   batch_q,
                }
            print(f"{_ts()} [text2insight] Pending-batch Q{choice} served for user={user}")
            return
        # Number out of range — fall through to normal pipeline

    # ── Download request (neutralised — one-shot delivery makes this redundant) ─
    # Uncomment to re-enable the manual download command.
    # if is_download_request(text):
    #     if not last:
    #         client.chat_postMessage(
    #             channel=channel,
    #             text=(
    #                 f"<@{user}> The download feature is available after you ask a data question. "
    #                 f"Go ahead and ask me something — once I answer, reply with *download* to get the results as a CSV!"
    #             )
    #         )
    #         return
    #
    #     # Grab and clear before upload so concurrent duplicate events can't both proceed
    #     csv_files = last.get("csv_files", [])
    #     _last_interaction[user]["csv_files"] = []
    #     _last_interaction[user]["csv_string"] = ""  # backward-compat clear
    #     if not csv_files:
    #         client.chat_postMessage(
    #             channel=channel,
    #             text=(
    #                 f"<@{user}> Looks like you've already downloaded that one! "
    #                 f"Ask me another question — once I answer, reply with *download* to get the fresh results as a CSV."
    #             )
    #         )
    #         return
    #
    #     failed = 0
    #     for i, cf in enumerate(csv_files, 1):
    #         slug     = re.sub(r'[^a-z0-9]+', '_', cf["question"].lower()[:30]).strip('_')
    #         filename = f"text2insight_q{i}_{slug}.csv" if len(csv_files) > 1 else "text2insight_data.csv"
    #         try:
    #             client.files_upload_v2(
    #                 channel=channel,
    #                 content=csv_string_to_bytes(cf["csv_string"]),
    #                 filename=filename,
    #                 title=f"Q{i}: {cf['question'][:60]}" if len(csv_files) > 1 else "text2insight Data Export",
    #             )
    #             print(f"{_ts()} [text2insight] CSV {i}/{len(csv_files)} uploaded for user={user}")
    #             if cf.get("log_id"):
    #                 mark_csv_downloaded(cf["log_id"])
    #         except Exception as e:
    #             failed += 1
    #             print(f"{_ts()} [text2insight] CSV {i} upload failed: {e}")
    #
    #     if failed:
    #         client.chat_postMessage(
    #             channel=channel,
    #             text=f"<@{user}> {failed} file(s) couldn't be uploaded — try again."
    #         )
    #
    #     user_info = get_user_info(client, user)
    #     log_interaction(
    #         user_id=user, email_id=user_info.get("email_id", ""),
    #         full_name=user_info.get("full_name", ""),
    #         raw_prompt=raw_prompt, question_asked=text,
    #         question_answered=f"CSV downloaded for: {last.get('question', '')}",
    #         status="failed" if failed else "success",
    #         interaction_type="download",
    #         spellcheck_applied=spellcheck_applied,
    #         corrected_prompt=text if spellcheck_applied else None,
    #     )
    #     return

    # ── Explain request (neutralised — one-shot delivery makes this redundant) ──
    # Uncomment to re-enable the manual explain command.
    # if is_explain_request(text):
    #     explain_items = last.get("explain_items", []) if last else []
    #     if not explain_items and last and last.get("results"):
    #         explain_items = [{"results": last["results"], "question": last.get("question", "")}]
    #     if not explain_items:
    #         client.chat_postMessage(
    #             channel=channel,
    #             text=(
    #                 f"<@{user}> The explain feature works after you ask a data question. "
    #                 f"Ask me something first — then reply with *explain* for a detailed analysis."
    #             )
    #         )
    #         return
    #
    #     n = len(explain_items)
    #     print(f"{_ts()} [text2insight] Generating {n} explanation(s) for user={user}")
    #     ts_thinking = client.chat_postMessage(
    #         channel=channel,
    #         text=f"<@{user}> ⏳ *Analysing {'your data' if n == 1 else f'{n} questions'}...*"
    #     )["ts"]
    #     user_info = get_user_info(client, user)
    #     for i, item in enumerate(explain_items):
    #         q_text      = item["question"]
    #         results     = item["results"]
    #         explanation = generate_explanation(q_text, results)
    #         header      = f"🔍 *{'Detailed breakdown' if n == 1 else f'Q{i+1} breakdown'}:* _{q_text}_\n\n"
    #         full_expl   = f"{header}{explanation}"
    #         if i == 0:
    #             client.chat_update(channel=channel, ts=ts_thinking, text=f"<@{user}> {full_expl}")
    #         else:
    #             client.chat_postMessage(channel=channel, text=f"<@{user}> {full_expl}")
    #         log_interaction(
    #             user_id=user, email_id=user_info.get("email_id", ""),
    #             full_name=user_info.get("full_name", ""),
    #             raw_prompt=raw_prompt, question_asked=q_text,
    #             question_answered=full_expl,
    #             status="success", interaction_type="explain",
    #             rows_returned=len(results),
    #             spellcheck_applied=spellcheck_applied,
    #             corrected_prompt=text if spellcheck_applied else None,
    #         )
    #     print(f"{_ts()} [text2insight] Explanation(s) posted for user={user}")
    #     return

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
    # (last was already read at the top of process_message for the pending-batch handler)
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
            csv_downloaded="yes" if r.get("csv_string") else "no",
            spellcheck_applied=spellcheck_applied,
            corrected_prompt=text if spellcheck_applied else None,
            viz_spec_json=r.get("viz_spec_json"),
            explain_text=r.get("explain_text"),
        )
        if r["status"] in ("success", "cache_hit") and log_id:
            update_cache_log_id(questions[0], log_id)
        if r["status"] == "success" and log_id and r["sql"]:
            learn_pattern(questions[0], r["sql"], log_id)

        with _last_interaction_lock:
            _last_interaction[user] = {
                "results":      r["results"],
                "csv_string":   r["csv_string"],
                "log_id":       log_id,
                "question":     questions[0],
                "csv_files":    [{"csv_string": r["csv_string"], "question": questions[0], "log_id": log_id}]
                                if r["csv_string"] else [],
                "explain_items": [{"results": r["results"], "question": questions[0]}]
                                 if r["results"] else [],
            }

        # One-shot delivery: summary → CSV → chart → explain
        _deliver(client, user, channel, questions[0], r, ts)

    else:
        # ── Multi-question ────────────────────────────────────────────────────
        print(f"{_ts()} [text2insight] Multi-question: {len(questions)} questions")

        # ── All-cache-hit fast path ───────────────────────────────────────────
        # Pre-check every question; if all hit, answer Q1 fully and offer Q2-QN
        # as a numbered menu (no parallel SQL execution needed).
        print(f"{_ts()} [text2insight] Pre-checking cache for {len(questions)} questions...")
        pre_cache = [get_cached(q) for q in questions]
        all_hit   = all(c is not None for c in pre_cache)

        if all_hit:
            print(f"{_ts()} [text2insight] All-cache-hit — answering Q1, offering Q2-QN menu")
            q1  = questions[0]
            msg = client.chat_postMessage(
                channel=channel,
                text=_progress_bar(10, f"Q1: {q1[:50]}..."),
            )
            ts1 = msg["ts"]
            r1  = _answer_with_progress(client, channel, ts1, q1, idx=1)

            log_id1 = log_interaction(
                user_id=user, email_id=email, full_name=full_name,
                raw_prompt=raw_prompt, question_asked=q1,
                question_answered=r1["reply"],
                status=r1["status"], interaction_type="data_query",
                generated_sql=r1["sql"] or None,
                result_json=r1.get("result_json"),
                generated_csv=r1["csv_string"] or None,
                failure_reason=r1.get("failure_reason") or None,
                similarity_matched_id=r1.get("similarity_matched_id"),
                similarity_score=r1.get("similarity_score"),
                self_learned=r1["status"] == "success",
                latency_ms=r1["latency_ms"],
                rows_returned=r1.get("rows_returned", 0),
                anomaly_count=r1.get("anomaly_count", 0),
                cached=r1["cached"],
                csv_downloaded="yes" if r1.get("csv_string") else "no",
                spellcheck_applied=spellcheck_applied,
                corrected_prompt=text if spellcheck_applied else None,
                viz_spec_json=r1.get("viz_spec_json"),
                explain_text=r1.get("explain_text"),
            )
            if log_id1:
                update_cache_log_id(q1, log_id1)

            # Deliver Q1: summary → CSV → chart → explain
            _deliver(client, user, channel, q1, r1, ts1, q_label="Q1")

            # Post numbered menu for Q2-QN
            remaining_qs  = questions[1:]
            options_lines = "\n".join(
                f"  *{i+2}.* {q[:80]}" for i, q in enumerate(remaining_qs)
            )
            client.chat_postMessage(
                channel=channel,
                text=(
                    f"<@{user}> 📋 *I have answers ready for your other questions:*\n"
                    f"{options_lines}\n\n"
                    f"Reply with a number (*2*–*{len(questions)}*) to get the full answer."
                )
            )

            with _last_interaction_lock:
                _last_interaction[user] = {
                    "results":      r1.get("results", []),
                    "csv_string":   r1["csv_string"],
                    "log_id":       log_id1,
                    "question":     q1,
                    "csv_files":    [{"csv_string": r1["csv_string"], "question": q1, "log_id": log_id1}]
                                    if r1["csv_string"] else [],
                    "explain_items": [{"results": r1.get("results", []), "question": q1}]
                                     if r1.get("results") else [],
                    "pending_batch": [{"question": q} for q in questions[1:]],
                }
            return

        # ── Normal parallel execution ─────────────────────────────────────────
        print(f"{_ts()} [text2insight] Parallel execution ({len(questions)} questions)")

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

        future_to_idx: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for idx, (q, ts_i) in enumerate(zip(questions, bar_ts)):
                f = executor.submit(_answer_with_progress, client, channel, ts_i, q, idx + 1)
                future_to_idx[f] = idx

            # Phase 1 — show each summary the instant its worker finishes.
            # CSV + chart + explain are delivered in Phase 2 (post-executor loop)
            # so slow Slack API calls don't delay subsequent question summaries.
            try:
                for future in concurrent.futures.as_completed(future_to_idx, timeout=300):
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
                            chart_bytes=None, viz_spec_json=None, explain_text=None,
                        )
                    ordered_results[idx] = r
                    # Replace progress bar with summary immediately
                    client.chat_update(
                        channel=channel, ts=ts_i,
                        text=f"<@{user}> {r['reply']}",
                    )
                    print(f"{_ts()} [text2insight] Q{idx+1} summary shown ({r['status']})")

            except concurrent.futures.TimeoutError:
                print(f"{_ts()} [text2insight] Parallel timeout — filling remaining with error")
                for future, idx in future_to_idx.items():
                    if ordered_results[idx] is None:
                        ordered_results[idx] = dict(
                            reply=f"*{idx+1}.* ⏱ Timed out — try asking separately.",
                            results=[], csv_string="", result_json=None,
                            status="failed", sql="", failure_reason="timeout",
                            latency_ms=300000, cached=False,
                            anomaly_count=0, rows_returned=0,
                            similarity_matched_id=None, similarity_score=None,
                            chart_bytes=None, viz_spec_json=None, explain_text=None,
                        )
                        client.chat_update(
                            channel=channel, ts=bar_ts[idx],
                            text=f"<@{user}> *{idx+1}.* ⏱ Timed out — try asking separately.",
                        )

        # Pre-set _last_interaction (log_id=None) before the slow logging loop;
        # log_ids are backfilled into csv_files entries after each log_interaction call.
        csv_files: list[dict] = [
            {"csv_string": r["csv_string"], "question": questions[idx], "log_id": None}
            for idx, r in enumerate(ordered_results)
            if r and r.get("csv_string")
        ]
        explain_items_early: list[dict] = [
            {"results": r["results"], "question": questions[idx]}
            for idx, r in enumerate(ordered_results)
            if r and r.get("results")
        ]
        _last_question_early = next(
            (questions[idx] for idx in range(len(ordered_results) - 1, -1, -1)
             if ordered_results[idx] and ordered_results[idx].get("csv_string")), ""
        )
        _last_results_early  = next(
            (ordered_results[idx]["results"]
             for idx in range(len(ordered_results) - 1, -1, -1)
             if ordered_results[idx] and ordered_results[idx].get("results")), []
        )
        with _last_interaction_lock:
            _last_interaction[user] = {
                "results":       _last_results_early,
                "csv_string":    csv_files[-1]["csv_string"] if csv_files else "",
                "log_id":        None,
                "question":      _last_question_early,
                "csv_files":     csv_files,
                "explain_items": explain_items_early,
            }

        # Phase 2 — deliver CSV + chart + explain in original question order,
        # then log each interaction.  Summaries were already shown in Phase 1.
        for idx, r in enumerate(ordered_results):
            if r is None:
                continue
            q = questions[idx]

            # 2a. Attach CSV
            if r.get("csv_string"):
                try:
                    slug  = re.sub(r'[^a-z0-9]+', '_', q.lower()[:30]).strip('_')
                    client.files_upload_v2(
                        channel=channel,
                        content=csv_string_to_bytes(r["csv_string"]),
                        filename=f"text2insight_Q{idx+1}_{slug}.csv",
                        title=q[:70],
                    )
                except Exception as _e:
                    print(f"{_ts()} [text2insight] Q{idx+1} CSV upload failed: {_e}")

            # 2b. Attach chart
            if r.get("chart_bytes"):
                try:
                    client.files_upload_v2(
                        channel=channel,
                        content=r["chart_bytes"],
                        filename=f"insight_q{idx+1}.png",
                        title=q[:70],
                    )
                except Exception as _e:
                    print(f"{_ts()} [Chart] Q{idx+1} upload failed: {_e}")
            elif r.get("status") in ("success", "cache_hit"):
                try:
                    client.chat_postMessage(
                        channel=channel,
                        text=f"<@{user}> _Q{idx+1}: Couldn't render a chart for this query._",
                    )
                except Exception:
                    pass

            # 2c. Post explain analysis
            if r.get("explain_text"):
                try:
                    client.chat_postMessage(
                        channel=channel,
                        text=(
                            f"<@{user}> 🔍 *Q{idx+1} Detailed breakdown:*"
                            f" _{q}_\n\n{r['explain_text']}"
                        )
                    )
                except Exception as _e:
                    print(f"{_ts()} [text2insight] Q{idx+1} explain post failed: {_e}")

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
                csv_downloaded="yes" if r.get("csv_string") else "no",
                spellcheck_applied=spellcheck_applied,
                corrected_prompt=text if spellcheck_applied else None,
                viz_spec_json=r.get("viz_spec_json"),
                explain_text=r.get("explain_text"),
            )
            if r["status"] in ("success", "cache_hit") and log_id:
                update_cache_log_id(q, log_id)
            if r["status"] == "success" and log_id and r["sql"]:
                learn_pattern(q, r["sql"], log_id)

            with _last_interaction_lock:
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
    # Only handle DMs here — channel messages come through handle_mention
    # This prevents double-processing when both handlers fire for @mention events
    if message.get("channel_type") != "im":
        return
    if message.get("bot_id") or message.get("subtype"):
        return
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
