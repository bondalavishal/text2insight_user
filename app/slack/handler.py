"""
handler.py — Phase 7 update
Changes from Phase 6:
  - handle_question() now returns (reply, results, csv_string) instead of just reply
  - Cache hit no longer shows similarity score to users
  - Download footer added to all data responses
  - Databricks interaction logging moved to main.py (needs Slack client for user info)
"""

import re
import time
import csv
import io
import httpx

from app.llm.intent import classify_intent
from app.llm.sql_generator import generate_sql
from app.sql.guardrails import validate_sql, enforce_limit
from app.sql.connector import run_query
from app.eval.cache import get_cached, save_to_cache, cache_stats
from app.eval.logger import log, get_stats

import os
from cerebras.cloud.sdk import Cerebras as _Cerebras
_cerebras_client  = _Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
CEREBRAS_MODEL    = "qwen-3-235b-a22b-instruct-2507"
OLLAMA_URL        = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL      = "mannix/defog-llama3-sqlcoder-8b"

DOWNLOAD_FOOTER = "\n\n💾 *Want the full data?* Reply with *download* to get a CSV."

# ── Pre-flight unanswerable patterns ─────────────────────────────────────────
UNANSWERABLE_PATTERNS = [
    (
        r'seller.{0,40}(improv|trend|over time|month.by.month|histor)'
        r'|(improv|trend|over time).{0,40}seller'
        r'|seller.{0,30}review.{0,30}(over time|trend|month|improv)',
        "vw_seller_metrics has no time dimension — seller metrics are lifetime aggregates only."
    ),
]

STATS_PATTERN = re.compile(
    r'(insightbot|bot).{0,20}(stat|metric|performance|pass rate)', re.I)

# ── Download trigger words ────────────────────────────────────────────────────
# "download" is shown to users in the footer
# others are god-mode silent triggers for dev/prod
DOWNLOAD_TRIGGERS = ["download", "csv", "export", "give me the data"]

# ── Anomaly thresholds ────────────────────────────────────────────────────────
DELIVERY_THRESHOLD     = 20.0
CANCEL_THRESHOLD       = 5.0
REVENUE_DROP_THRESHOLD = 10.0
REVIEW_THRESHOLD       = 3.0

_PADDING = [
    "There is no message", "No additional information",
    "Delivery durations are not", "The data does not contain",
    "No message to", "There are no messages",
    "If you have any further", "Please note that", "Please feel free"
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_download_request(text: str) -> bool:
    """Returns True if the message is a download/csv/export request."""
    t = text.lower().strip()
    return any(trigger in t for trigger in DOWNLOAD_TRIGGERS)


def results_to_csv_string(results: list[dict]) -> str:
    """Converts query results to a CSV string."""
    if not results:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=results[0].keys())
    writer.writeheader()
    writer.writerows(results)
    return output.getvalue()


def _check_unanswerable(q: str):
    for pattern, reason in UNANSWERABLE_PATTERNS:
        if re.search(pattern, q.lower()):
            return reason
    return None


def _generate_sql_with_overrides(question: str) -> str:
    return generate_sql(question)


def detect_anomalies(question: str, results: list[dict]) -> list[str]:
    if not results:
        return []
    flags, q = [], question.lower()
    keys = list(results[0].keys())

    for col in [k for k in keys if "delivery" in k.lower() and "day" in k.lower()]:
        for row in results:
            try:
                if float(row.get(col)) > DELIVERY_THRESHOLD:
                    state = row.get("customer_state", "?")
                    flags.append(
                        f"⚠️ *Anomaly:* {state} avg delivery is {row.get(col)} days"
                        f" — exceeds {int(DELIVERY_THRESHOLD)}-day threshold.")
            except: pass

    for col in [k for k in keys if "cancel" in k.lower() and
                any(w in k.lower() for w in ["pct", "rate", "percent"])]:
        for row in results:
            try:
                fval = float(row.get(col))
                if fval > CANCEL_THRESHOLD:
                    p = row.get("year_month", row.get("month", ""))
                    flags.append(
                        f"⚠️ *Anomaly:* Cancellation rate{f' in {p}' if p else ''}"
                        f" is {round(fval,1)}% — exceeds {int(CANCEL_THRESHOLD)}%.")
            except: pass

    for col in [k for k in keys if any(w in k.lower()
                for w in ["growth", "pct", "drop", "mom", "change"])]:
        for row in results:
            try:
                fval = float(row.get(col))
                if fval < -REVENUE_DROP_THRESHOLD:
                    p = row.get("year_month", row.get("month", ""))
                    flags.append(
                        f"⚠️ *Anomaly:* Revenue dropped {abs(round(fval,1))}%"
                        f"{f' in {p}' if p else ''}"
                        f" — exceeds {int(REVENUE_DROP_THRESHOLD)}% threshold.")
            except: pass

    if (any(k for k in keys if "review" in k.lower() and "score" in k.lower())
            and ("seller" in q or "review" in q)):
        col = next(k for k in keys if "review" in k.lower() and "score" in k.lower())
        for row in results:
            try:
                fval = float(row.get(col))
                if fval < REVIEW_THRESHOLD:
                    s = row.get("seller_id", "")
                    flags.append(
                        f"⚠️ *Anomaly:* Seller{f' ({s[:8]}...)' if s else ''}"
                        f" review is {round(fval,2)} — below {REVIEW_THRESHOLD}.")
            except: pass

    seen, unique = set(), []
    for f in flags:
        if f[:60] not in seen:
            seen.add(f[:60]); unique.append(f)
        if len(unique) >= 3: break
    return unique


def _clean_summary(text: str) -> str:
    for phrase in _PADDING:
        idx = text.find(phrase)
        if idx != -1: text = text[:idx].strip()
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    text = " ".join(sentences[:3])
    if text and text[-1] not in ".!?": text += "."
    return text.strip()


SUMMARY_PROMPT = """You are a business analytics assistant.
A user asked: {question}
Data: {results}
Write a clear 2-3 sentence answer in plain English.
Use actual numbers. Prefix R$ for monetary values. Say 'days' for delivery.
Stop after 3 sentences. No disclaimers.
Answer:"""


def summarise_results(question: str, results: list[dict]) -> str:
    if not results:
        return "The query returned no results."
    if len(results) == 1 and "message" in results[0]:
        return results[0]["message"]
    sample       = results[:20]
    results_text = "\n".join(str(r) for r in sample)
    if len(results) > 20:
        results_text += f"\n... and {len(results)-20} more rows."
    prompt = SUMMARY_PROMPT.format(question=question, results=results_text)

    # Primary: Cerebras
    try:
        resp = _cerebras_client.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=100,
            timeout=30,
        )
        print("[LLM] Summary via Cerebras")
        return _clean_summary(resp.choices[0].message.content.strip())
    except Exception as e:
        print(f"[LLM] Cerebras summary failed ({e}) — falling back to Ollama")

    # Fallback: Ollama
    try:
        response = httpx.post(OLLAMA_URL, json={
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0, "num_predict": 100}
        }, timeout=120)
        print("[LLM] Summary via Ollama (fallback)")
        return _clean_summary(response.json()["response"].strip())
    except Exception as e:
        print(f"[LLM] Ollama summary also failed ({e})")
        return "Summary unavailable — here is the raw data above."


def _split_questions(text: str) -> list[str]:
    for splitter in [r'\n?\s*\d+[\.)]\s+', r'\n\s*[-•]\s+']:
        parts = [p.strip() for p in re.split(splitter, text)
                 if p.strip() and len(p.strip()) > 10]
        if len(parts) > 1: return parts
    lines = [l.strip() for l in text.split('\n')
             if l.strip() and len(l.strip()) > 10]
    if len(lines) > 1: return lines
    parts = [p.strip()+"?" for p in re.split(r'\?\s+', text)
             if p.strip() and len(p.strip()) > 10]
    if len(parts) > 1: return parts
    return [text.strip()]


# ── Main handler ──────────────────────────────────────────────────────────────

def handle_question(user_id: str, question: str) -> tuple:
    """
    Returns: (reply: str, results: list[dict], csv_string: str)
      - reply      → text to post in Slack
      - results    → raw Databricks rows (empty list if not a data query)
      - csv_string → CSV string of results (empty string if not a data query)
    """
    print(f"\n[InsightBot] User: {question}")
    start = time.time()

    # ── Cache check ───────────────────────────────────────────────────────────
    cached = get_cached(question)
    if cached:
        log(question=question, sql=cached["sql"],
            latency_sec=round(time.time()-start, 2),
            cached=True, status="cache_hit")
        # No similarity score shown to user — clean answer only
        reply = f"<@{user_id}> {cached['answer']}{DOWNLOAD_FOOTER}"
        return reply, [], ""

    # ── Intent ────────────────────────────────────────────────────────────────
    intent = classify_intent(question)
    if intent == "greeting":
        reply = (f"Hi <@{user_id}>! 👋 I'm InsightBot — ask me anything about "
                 f"orders, revenue, sellers, products or delivery performance.")
        return reply, [], ""
    if intent == "out_of_scope":
        reply = (f"Sorry <@{user_id}>, I can only answer questions about "
                 f"business data — orders, revenue, sellers, products, delivery.")
        return reply, [], ""

    # ── Pre-flight ────────────────────────────────────────────────────────────
    reason = _check_unanswerable(question)
    if reason:
        log(question=question, latency_sec=round(time.time()-start, 2),
            status="blocked", error=reason)
        reply = f"<@{user_id}> Sorry, that can't be answered: {reason}"
        return reply, [], ""

    # ── SQL generation ────────────────────────────────────────────────────────
    sql = _generate_sql_with_overrides(question)
    print(f"[InsightBot] SQL: {sql[:80]}...")

    is_valid, reason = validate_sql(sql)
    if not is_valid:
        log(question=question, sql=sql,
            latency_sec=round(time.time()-start, 2),
            status="fail", error=reason)
        reply = f"Sorry <@{user_id}>, couldn't generate a safe query. Try rephrasing."
        return reply, [], ""

    sql = enforce_limit(sql)

    # ── Databricks execution ──────────────────────────────────────────────────
    try:
        results = run_query(sql)
        print(f"[InsightBot] Rows: {len(results)}")
    except Exception as e:
        log(question=question, sql=sql,
            latency_sec=round(time.time()-start, 2),
            status="fail", error=str(e))
        reply = f"Sorry <@{user_id}>, query error. Try rephrasing."
        return reply, [], ""

    # ── Anomaly detection ─────────────────────────────────────────────────────
    flags  = detect_anomalies(question, results)
    summary = summarise_results(question, results)

    # ── Build reply ───────────────────────────────────────────────────────────
    reply = summary
    if flags:
        reply += "\n" + "\n".join(flags)
    reply += DOWNLOAD_FOOTER

    # ── Generate CSV string ───────────────────────────────────────────────────
    csv_string = results_to_csv_string(results)

    # ── Cache + eval log ──────────────────────────────────────────────────────
    latency = round(time.time()-start, 2)
    save_to_cache(question, summary, sql)
    log(question=question, sql=sql, rows_returned=len(results),
        latency_sec=latency, status="pass", anomalies=len(flags))

    return f"<@{user_id}> {reply}", results, csv_string
