"""
run_test.py — wipe all logs then run the 5 analytical questions through the pipeline.
Usage:  python run_test.py
"""

import os, sys, time, traceback
from dotenv import load_dotenv
load_dotenv()

from datetime import datetime
_ts = lambda: datetime.now().strftime("%H:%M:%S")

# ── 1. WIPE ChromaDB collections ─────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 1 — Wiping ChromaDB (cache + query lib)")
print("="*60)

import chromadb
CHROMA_DIR = os.path.join("app", "rag", "chroma_db")
_chroma = chromadb.PersistentClient(path=CHROMA_DIR)

for col_name in ("text2insight_cache", "text2insight_query_lib"):
    try:
        col = _chroma.get_collection(col_name)
        count = col.count()
        _chroma.delete_collection(col_name)
        print(f"  ✓ Deleted {col_name} ({count} entries)")
    except Exception as e:
        print(f"  – {col_name} not found or already empty ({e})")

# ── 2. WIPE local eval log CSV ────────────────────────────────────────────────
print("\nSTEP 2 — Wiping local eval_log.csv")
eval_log = os.path.join("app", "eval", "eval_log.csv")
if os.path.exists(eval_log):
    os.remove(eval_log)
    print(f"  ✓ Deleted {eval_log}")
else:
    print(f"  – {eval_log} not found, nothing to delete")

# ── 3. WIPE Databricks logs ────────────────────────────────────────────────────
print("\nSTEP 3 — Wiping Databricks query log for test questions")
from app.sql.connector import run_query

TEST_QUESTIONS = [
    "Find sellers in the top quartile of order volume but bottom quartile of review score — which categories do they sell in?",
    "Which states have both above-average revenue and below-average turnaround time — and how do their review scores compare to the platform average?",
    "Show monthly revenue acceleration — months where the month over month growth rate itself improved over the prior month's growth rate",
    "Build a category risk matrix: for each category show total orders, cancel rate, average review score, and average turnaround time — rank them by a risk score where high cancel rate and low review score = highest risk",
    "Which sellers make up the first 80 percent of total gross merchandise value (Pareto cutoff)? Show how many sellers that is and their average review score versus the rest",
]

wipe_sql = """
DELETE FROM default.text2insight_user_query_log
WHERE question_asked IN ({placeholders})
""".format(
    placeholders=", ".join(f"'{q.replace(chr(39), chr(39)+chr(39))}'" for q in TEST_QUESTIONS)
)

try:
    run_query(wipe_sql)
    print(f"  ✓ Deleted matching Databricks rows for {len(TEST_QUESTIONS)} questions")
except Exception as e:
    print(f"  ⚠ Databricks wipe error (non-fatal): {e}")

# Also wipe spellcheck-expanded variants
try:
    run_query("""
        DELETE FROM default.text2insight_user_query_log
        WHERE question_asked LIKE '%top quartile%'
           OR question_asked LIKE '%month over month%'
           OR question_asked LIKE '%risk matrix%'
           OR question_asked LIKE '%Pareto%'
           OR question_asked LIKE '%pareto%'
           OR question_asked LIKE '%gross merchandise value%'
    """)
    print("  ✓ Deleted Databricks rows matching keyword patterns")
except Exception as e:
    print(f"  ⚠ Keyword wipe error (non-fatal): {e}")

# ── 4. RELOAD ChromaDB module singletons (clear cached handles) ───────────────
print("\nSTEP 4 — Reloading module singletons")
import importlib, app.eval.cache as _cache_mod, app.rag.retriever as _ret_mod
_cache_mod._client = None; _cache_mod._collection = None
_ret_mod._client   = None; _ret_mod._collection   = None
print("  ✓ Cache and retriever singletons cleared")

# ── 5. RUN the 5 questions ─────────────────────────────────────────────────────
print("\n" + "="*60)
print("STEP 5 — Running 5 analytical questions")
print("="*60)

from app.llm.sql_generator import generate_sql
from app.sql.guardrails    import validate_sql, enforce_limit
from app.slack.handler     import summarise_results, detect_anomalies, results_to_csv_string
from app.llm.spellcheck    import correct_prompt

results_summary = []

for i, raw_q in enumerate(TEST_QUESTIONS, 1):
    print(f"\n{'─'*60}")
    print(f"Q{i}: {raw_q}")
    t0 = time.time()

    # Spellcheck
    q = correct_prompt(raw_q)
    if q != raw_q:
        print(f"  [Spellcheck] → {q}")

    try:
        # Generate SQL
        print(f"  {_ts()} Generating SQL...")
        sql = generate_sql(q)
        print(f"  SQL: {sql[:120]}...")

        # Validate
        is_valid, reason, failure_type = validate_sql(sql)
        if not is_valid:
            print(f"  ✗ VALIDATION FAIL: {reason}")
            results_summary.append({"q": i, "status": "FAIL", "reason": f"Validation: {reason}", "sql": sql})
            continue

        sql = enforce_limit(sql)

        # Execute
        print(f"  {_ts()} Running on Databricks...")
        rows = run_query(sql)
        print(f"  {_ts()} Rows returned: {len(rows)}")

        if rows:
            print(f"  Sample row: {dict(list(rows[0].items())[:4])}")

        # Summarize
        summary = summarise_results(q, rows)
        flags   = detect_anomalies(q, rows)
        latency = round(time.time() - t0, 2)

        print(f"  Summary: {summary}")
        if flags:
            print(f"  Anomalies: {flags}")
        print(f"  ✓ PASS — {len(rows)} rows, {latency}s")

        results_summary.append({
            "q": i, "status": "PASS", "rows": len(rows),
            "latency_s": latency, "summary": summary[:80],
        })

    except Exception as e:
        latency = round(time.time() - t0, 2)
        tb = traceback.format_exc().strip().split('\n')[-1]
        print(f"  ✗ ERROR after {latency}s: {tb}")
        results_summary.append({"q": i, "status": "ERROR", "reason": str(e), "sql": sql if 'sql' in dir() else ""})

# ── 6. FINAL REPORT ────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("FINAL REPORT")
print("="*60)
passed = sum(1 for r in results_summary if r["status"] == "PASS")
for r in results_summary:
    icon = "✓" if r["status"] == "PASS" else "✗"
    if r["status"] == "PASS":
        print(f"  {icon} Q{r['q']} PASS — {r['rows']} rows, {r['latency_s']}s")
    else:
        print(f"  {icon} Q{r['q']} {r['status']}: {r.get('reason','')[:80]}")

print(f"\n  Pass rate: {passed}/{len(TEST_QUESTIONS)}")
print("="*60)
