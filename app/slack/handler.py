"""
handler.py — shared utilities used by main.py
  - summarise_results, detect_anomalies, _split_questions, _check_unanswerable
  - results_to_csv_string, is_download_request
  - Re-exports: log, get_stats, cache_stats, get_cached, save_to_cache
"""

import re
import csv
import io
import httpx
import concurrent.futures
from datetime import datetime

_ts = lambda: datetime.now().strftime("%H:%M:%S")

from app.llm.sql_generator import generate_sql
from app.eval.cache import get_cached, save_to_cache, cache_stats
from app.eval.logger import log, get_stats

import os
from cerebras.cloud.sdk import Cerebras as _Cerebras
from app.llm.cerebras_breaker import is_open, record_failure, record_success
_cerebras_client  = _Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
CEREBRAS_MODEL    = "qwen-3-235b-a22b-instruct-2507"
OLLAMA_URL        = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL      = "mannix/defog-llama3-sqlcoder-8b"

# ── Groq / OpenRouter fallbacks (mirrors sql_generator.py chain) ─────────────
_GROQ_API_KEY        = os.getenv("GROQ_API_KEY")
_GROQ_URL            = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_SUMMARY_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
]
_OPENROUTER_API_KEY        = os.getenv("OPENROUTER_API_KEY")
_OPENROUTER_URL            = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_SUMMARY_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

# ── Explain trigger words ─────────────────────────────────────────────────────
EXPLAIN_TRIGGERS = ["explain", "deep dive", "analyse this", "analyze this", "tell me more"]

# ── Pre-flight unanswerable patterns ─────────────────────────────────────────
UNANSWERABLE_PATTERNS = [
    (
        r'seller.{0,40}(improv|trend|over time|month[\s\-]by[\s\-]month|histor)'
        r'|(improv|trend|over time).{0,40}seller'
        r'|seller.{0,30}review.{0,30}(over time|trend|month|improv)',
        "vw_seller_metrics has no time dimension — seller metrics are lifetime aggregates only."
    ),
]

STATS_PATTERN = re.compile(
    r'(text2insight|bot).{0,20}(stat|metric|performance|pass rate)', re.I)

# ── Download trigger words ────────────────────────────────────────────────────
# "download" is shown to users in the footer
# others are god-mode silent triggers for dev/prod
DOWNLOAD_TRIGGERS = ["download", "csv", "give me the data"]

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
    """Returns True if the message is a download/csv/export request.
    Hard cap at 40 chars — download commands are short; data questions are not."""
    t = text.lower().strip()
    if len(t) > 40:
        return False
    return any(trigger in t for trigger in DOWNLOAD_TRIGGERS)


def is_explain_request(text: str) -> bool:
    """Returns True if the message is a request for a deep-dive explanation.
    Hard cap at 40 chars — explain commands are short; data questions are not."""
    t = text.lower().strip()
    if len(t) > 40:
        return False
    return any(trigger in t for trigger in EXPLAIN_TRIGGERS)


EXPLAIN_PROMPT = """You are a senior business analyst presenting findings to a strategy team.

The user asked: {question}

The full dataset returned ({row_count} rows):
{results}

Deliver a structured analysis using EXACTLY the formatting below. Output the section headers in *bold* exactly as shown — do NOT use ## or ### markdown.

*Overall Picture*
2–3 sentences on what this data actually reveals. Do NOT restate the question or repeat filter criteria. State the dominant pattern, gap, or trend visible in the numbers.

*Key Findings*
• [Finding with specific number from the data]
• [Finding with specific number from the data]
• [Finding with specific number from the data]
3–5 bullets. Each must cite a real number. Focus on the largest gaps, rankings, and contrasts between top and bottom performers.

*Outliers & Anomalies*
• [Outlier: name the data point, state the magnitude of deviation, give a plausible business reason]
Identify 1–3 data points that deviate most from the norm.

*Business Implications*
2–3 sentences. Connect the pattern to a real business problem — customer trust, revenue at risk from churn, operational bottleneck. Be specific about which segments are affected. Do NOT claim an entire category's GMV is "at risk" — only a portion is.

*Recommended Actions*
1. [Specific action naming the category/metric it addresses]
2. [Specific action naming the category/metric it addresses]
3. [Specific action naming the category/metric it addresses]
3–5 numbered, prioritized actions. Do NOT address actions to any specific role, team, or job title — write in first-person recommendation style ("Investigate...", "Prioritise...", "Consider..."). Each must name the specific category or metric and the outcome to target.

Formatting rules — follow strictly:
- Section headers: *bold* exactly as shown, never ##
- Bullet points: use • for Key Findings and Outliers
- Numbered list: use 1. 2. 3. for Recommended Actions
- Use actual numbers from the data only — never invent figures
- Prefix R$ for monetary values, say 'days' for delivery
- No disclaimers, no filler sentences, no restating the question"""


def generate_explanation(question: str, results: list[dict]) -> str:
    """
    Generates a structured business analyst deep-dive for the given question and results.
    Uses the same LLM fallback chain as summarise_results.
    """
    if not results:
        return "No data available to explain."

    results_text = "\n".join(str(r) for r in results)

    prompt = EXPLAIN_PROMPT.format(
        question=question,
        row_count=len(results),
        results=results_text,
    )

    # 1. Cerebras
    _CEREBRAS_EXPLAIN_TIMEOUT = 20
    if not is_open():
        def _cerebras_explain():
            return _cerebras_client.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            ).choices[0].message.content.strip()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                raw = ex.submit(_cerebras_explain).result(timeout=_CEREBRAS_EXPLAIN_TIMEOUT)
            record_success()
            print(f"{_ts()} [LLM] Explanation via Cerebras")
            return raw
        except concurrent.futures.TimeoutError:
            record_failure()
            print(f"{_ts()} [LLM] Cerebras explain timed out — trying Groq")
        except Exception as e:
            record_failure()
            print(f"{_ts()} [LLM] Cerebras explain failed ({e}) — trying Groq")
    else:
        print(f"{_ts()} [LLM] Cerebras circuit open — skipping to Groq")

    # 2. Groq
    if _GROQ_API_KEY:
        for model in _GROQ_SUMMARY_MODELS:
            try:
                r = httpx.post(
                    _GROQ_URL,
                    headers={"Authorization": f"Bearer {_GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0},
                    timeout=30,
                )
                if r.status_code in (429, 413):
                    print(f"{_ts()} [LLM] Groq {model} skipped ({r.status_code}) — next model")
                    continue
                r.raise_for_status()
                print(f"{_ts()} [LLM] Explanation via Groq ({model})")
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as ex:
                print(f"{_ts()} [LLM] Groq {model} failed ({ex}) — next model")

    # 3. OpenRouter
    if _OPENROUTER_API_KEY:
        for model in _OPENROUTER_SUMMARY_MODELS:
            try:
                r = httpx.post(
                    _OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {_OPENROUTER_API_KEY}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "https://text2insight.app",
                             "X-Title": "text2insight"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0},
                    timeout=45,
                )
                if r.status_code == 429:
                    print(f"{_ts()} [LLM] OpenRouter {model} rate-limited — next model")
                    continue
                r.raise_for_status()
                print(f"{_ts()} [LLM] Explanation via OpenRouter ({model})")
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as ex:
                print(f"{_ts()} [LLM] OpenRouter {model} failed ({ex}) — next model")

    # 4. Ollama
    try:
        response = httpx.post(OLLAMA_URL, json={
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0, "num_predict": -1}
        }, timeout=120)
        print(f"{_ts()} [LLM] Explanation via Ollama (last resort)")
        return response.json()["response"].strip()
    except Exception as e:
        print(f"{_ts()} [LLM] Ollama explain also failed ({e})")
        return "Explanation unavailable — see the data above."


_COMBINED_PROMPT = """You are a senior business analytics assistant.

The user asked: {question}

Full dataset ({row_count} rows):
{results}

Produce TWO outputs in EXACTLY this format — no deviation, no extra text outside the delimiters:

===SUMMARY===
2–3 sentence executive answer. State the dominant finding with specific numbers. No filler, no disclaimers.

===ANALYSIS===
*Overall Picture*
2–3 sentences on what this data actually reveals. Do NOT restate the question. State the dominant pattern, gap, or trend.

*Key Findings*
• [Finding with specific number from the data]
• [Finding with specific number from the data]
• [Finding with specific number from the data]
3–5 bullets. Each must cite a real number. Focus on the largest gaps, rankings, and contrasts.

*Outliers & Anomalies*
• [Outlier: name the data point, state the magnitude of deviation, give a plausible business reason]
1–3 data points that deviate most from the norm.

*Business Implications*
2–3 sentences. Connect the pattern to a real business problem. Be specific about which segments are affected.

*Recommended Actions*
1. [Specific action naming the category/metric and the outcome to target]
2. [Specific action]
3. [Specific action]
3–5 prioritized actions in first-person style ("Investigate...", "Prioritise...", "Consider...").

Formatting rules:
- Section headers: *bold* exactly as shown, never ##
- Bullet points: use • for Key Findings and Outliers
- Numbered list: 1. 2. 3. for Recommended Actions
- Use actual numbers from the data only — never invent figures
- Prefix R$ for monetary values, say 'days' for delivery
- No disclaimers, no filler, no restating the question"""


def _parse_combined(raw: str) -> tuple[str, str]:
    """Extract (summary, analysis) from a combined LLM response."""
    summary_m  = re.search(r'===SUMMARY===\s*(.*?)(?====ANALYSIS===)', raw, re.DOTALL)
    analysis_m = re.search(r'===ANALYSIS===\s*(.*?)$', raw, re.DOTALL)
    summary  = _clean_summary(summary_m.group(1).strip()) if summary_m else ""
    analysis = analysis_m.group(1).strip() if analysis_m else ""
    return summary, analysis


def summarise_and_explain(question: str, results: list[dict]) -> tuple[str, str]:
    """
    Single LLM call returning (summary, explain_text).
    Replaces separate summarise_results() + generate_explanation() calls.
    Falls back to the individual functions if the combined call fails or
    the response doesn't contain both delimiters.
    """
    if not results:
        return "The query returned no results.", ""

    results_text = "\n".join(str(r) for r in results)
    prompt = _COMBINED_PROMPT.format(
        question=question,
        row_count=len(results),
        results=results_text,
    )

    raw: str = ""

    # 1. Cerebras
    _TIMEOUT = 25
    if not is_open():
        def _cerebras_combined():
            return _cerebras_client.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            ).choices[0].message.content.strip()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                raw = ex.submit(_cerebras_combined).result(timeout=_TIMEOUT)
            record_success()
            print(f"{_ts()} [LLM] Summary+Explain via Cerebras")
        except concurrent.futures.TimeoutError:
            record_failure()
            print(f"{_ts()} [LLM] Cerebras combined timed out — trying Groq")
        except Exception as e:
            record_failure()
            print(f"{_ts()} [LLM] Cerebras combined failed ({e}) — trying Groq")
    else:
        print(f"{_ts()} [LLM] Cerebras circuit open — skipping to Groq")

    # 2. Groq
    if not raw and _GROQ_API_KEY:
        for model in _GROQ_SUMMARY_MODELS:
            try:
                r = httpx.post(
                    _GROQ_URL,
                    headers={"Authorization": f"Bearer {_GROQ_API_KEY}",
                             "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0},
                    timeout=45,
                )
                if r.status_code in (429, 413):
                    print(f"{_ts()} [LLM] Groq {model} skipped ({r.status_code}) — next model")
                    continue
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"].strip()
                print(f"{_ts()} [LLM] Summary+Explain via Groq ({model})")
                break
            except Exception as ex:
                print(f"{_ts()} [LLM] Groq {model} failed ({ex}) — next model")

    # 3. OpenRouter
    if not raw and _OPENROUTER_API_KEY:
        for model in _OPENROUTER_SUMMARY_MODELS:
            try:
                r = httpx.post(
                    _OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {_OPENROUTER_API_KEY}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "https://text2insight.app",
                             "X-Title": "text2insight"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0},
                    timeout=60,
                )
                if r.status_code == 429:
                    print(f"{_ts()} [LLM] OpenRouter {model} rate-limited — next model")
                    continue
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"].strip()
                print(f"{_ts()} [LLM] Summary+Explain via OpenRouter ({model})")
                break
            except Exception as ex:
                print(f"{_ts()} [LLM] OpenRouter {model} failed ({ex}) — next model")

    # 4. Ollama
    if not raw:
        try:
            resp = httpx.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL, "prompt": prompt,
                "stream": False, "options": {"temperature": 0, "num_predict": -1},
            }, timeout=120)
            raw = resp.json()["response"].strip()
            print(f"{_ts()} [LLM] Summary+Explain via Ollama (last resort)")
        except Exception as e:
            print(f"{_ts()} [LLM] All providers failed ({e})")

    if raw:
        summary, analysis = _parse_combined(raw)
        if summary and analysis:
            return summary, analysis
        # LLM responded but didn't use delimiters — treat whole response as analysis,
        # fall through to summarise_results() for the short summary
        print(f"{_ts()} [LLM] Combined response missing delimiters — extracting summary separately")
        if analysis:
            return summarise_results(question, results), analysis

    # Full fallback: separate calls
    print(f"{_ts()} [LLM] Combined call failed — falling back to separate calls")
    return summarise_results(question, results), generate_explanation(question, results)


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
            except (TypeError, ValueError):
                pass

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
            except (TypeError, ValueError):
                pass

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
            except (TypeError, ValueError):
                pass

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
            except (TypeError, ValueError):
                pass

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

    # 1. Cerebras (hard 10s wall-clock timeout)
    _CEREBRAS_SUMMARY_TIMEOUT = 10
    if not is_open():
        def _cerebras_summary():
            return _cerebras_client.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=100,
            ).choices[0].message.content.strip()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                raw = ex.submit(_cerebras_summary).result(timeout=_CEREBRAS_SUMMARY_TIMEOUT)
            result = _clean_summary(raw)
            record_success()
            print(f"{_ts()} [LLM] Summary via Cerebras")
            return result
        except concurrent.futures.TimeoutError:
            record_failure()
            print(f"{_ts()} [LLM] Cerebras summary timed out after {_CEREBRAS_SUMMARY_TIMEOUT}s — trying Groq")
        except Exception as e:
            record_failure()
            print(f"{_ts()} [LLM] Cerebras summary failed ({e}) — trying Groq")
    else:
        print(f"{_ts()} [LLM] Cerebras circuit open — skipping to Groq")

    # 2. Groq
    if _GROQ_API_KEY:
        for model in _GROQ_SUMMARY_MODELS:
            try:
                r = httpx.post(
                    _GROQ_URL,
                    headers={"Authorization": f"Bearer {_GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0, "max_tokens": 100},
                    timeout=30,
                )
                if r.status_code in (429, 413):
                    print(f"{_ts()} [LLM] Groq {model} skipped ({r.status_code}) — next model")
                    continue
                r.raise_for_status()
                print(f"{_ts()} [LLM] Summary via Groq ({model})")
                return _clean_summary(r.json()["choices"][0]["message"]["content"].strip())
            except Exception as ex:
                print(f"{_ts()} [LLM] Groq {model} failed ({ex}) — next model")

    # 3. OpenRouter
    if _OPENROUTER_API_KEY:
        for model in _OPENROUTER_SUMMARY_MODELS:
            try:
                r = httpx.post(
                    _OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {_OPENROUTER_API_KEY}",
                             "Content-Type": "application/json",
                             "HTTP-Referer": "https://text2insight.app",
                             "X-Title": "text2insight"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0, "max_tokens": 100},
                    timeout=45,
                )
                if r.status_code == 429:
                    print(f"{_ts()} [LLM] OpenRouter {model} rate-limited — next model")
                    continue
                r.raise_for_status()
                print(f"{_ts()} [LLM] Summary via OpenRouter ({model})")
                return _clean_summary(r.json()["choices"][0]["message"]["content"].strip())
            except Exception as ex:
                print(f"{_ts()} [LLM] OpenRouter {model} failed ({ex}) — next model")

    # 4. Ollama (last resort)
    try:
        response = httpx.post(OLLAMA_URL, json={
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0, "num_predict": 100}
        }, timeout=120)
        print("[LLM] Summary via Ollama (last resort)")
        return _clean_summary(response.json()["response"].strip())
    except Exception as e:
        print(f"{_ts()} [LLM] Ollama summary also failed ({e})")
        return "Summary unavailable — here is the raw data above."


def _split_questions(text: str) -> list[str]:
    for splitter in [r'\n?\s*\d+[\.)]\s+', r'\n\s*[-•]\s+']:
        parts = [p.strip() for p in re.split(splitter, text)
                 if p.strip() and len(p.strip()) > 10]
        if len(parts) > 1: return parts
    lines = [l.strip() for l in text.split('\n')
             if l.strip() and len(l.strip()) > 10]
    if len(lines) > 1: return lines
    # Only split on '?' when there are 3+ question marks — 2 is common in a
    # single analytical question with a follow-up sub-question (e.g. "...bucket
    # into X? Which bucket has the highest...?").  Users wanting exactly 2
    # separate questions should number them or put each on its own line.
    if text.count('?') >= 3:
        parts = [p.strip().rstrip("?")+"?" for p in re.split(r'\?\s+', text)
                 if p.strip() and len(p.strip()) > 10]
        if len(parts) > 1: return parts
    return [text.strip()]
