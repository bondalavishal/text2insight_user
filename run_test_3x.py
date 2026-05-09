"""
run_test_3x.py — wipe once, run the 5 questions 3 times, report SQL + results each time.
Usage:  python run_test_3x.py
"""

import os, sys, time, traceback, re
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
_ts = lambda: datetime.now().strftime("%H:%M:%S")

# ── Wipe once ─────────────────────────────────────────────────────────────────
import chromadb
CHROMA_DIR = os.path.join("app", "rag", "chroma_db")
_chroma = chromadb.PersistentClient(path=CHROMA_DIR)
for col_name in ("text2insight_cache", "text2insight_query_lib"):
    try: count = _chroma.get_collection(col_name).count(); _chroma.delete_collection(col_name); print(f"Wiped {col_name} ({count} entries)")
    except: print(f"{col_name} already empty")

eval_log = os.path.join("app", "eval", "eval_log.csv")
if os.path.exists(eval_log): os.remove(eval_log); print("Wiped eval_log.csv")

from app.sql.connector import run_query
try:
    run_query("""DELETE FROM default.text2insight_user_query_log WHERE
        question_asked LIKE '%top quartile%' OR question_asked LIKE '%bottom quartile%'
        OR question_asked LIKE '%above-average revenue%' OR question_asked LIKE '%above average revenue%'
        OR question_asked LIKE '%month over month%' OR question_asked LIKE '%risk matrix%'
        OR question_asked LIKE '%Pareto%' OR question_asked LIKE '%pareto%'
        OR question_asked LIKE '%gross merchandise value%'""")
    print("Wiped Databricks rows")
except Exception as e: print(f"Databricks wipe skipped: {e}")

# ── Questions ─────────────────────────────────────────────────────────────────
TEST_QUESTIONS = [
    "Find sellers in the top quartile of order volume but bottom quartile of review score — which categories do they sell in?",
    "Which states have both above-average revenue and below-average turnaround time — and how do their review scores compare to the platform average?",
    "Show monthly revenue acceleration — months where the month over month growth rate itself improved over the prior month's growth rate",
    "Build a category risk matrix: for each category show total orders, cancel rate, average review score, and average turnaround time — rank them by a risk score where high cancel rate and low review score = highest risk",
    "Which sellers make up the first 80 percent of total gross merchandise value (Pareto cutoff)? Show how many sellers that is and their average review score versus the rest",
]

# ── SQL validation helpers ────────────────────────────────────────────────────
def check_q1_sql(sql):
    """top quartile order ASC>=0.75, bottom quartile review ASC<=0.25"""
    issues = []
    if "review_rank <= 0.25" in sql and "avg_review_score DESC" in sql.lower():
        issues.append("WRONG direction: ORDER BY review DESC + <=0.25 selects TOP reviewers not bottom")
    if "grp" in sql.lower() and "case when" in sql.lower():
        issues.append("grp/CASE WHEN in Q1 — Pareto pattern leaked in")
    if "product_category_name_english" in sql and "product_category_translation" not in sql:
        issues.append("Missing JOIN to product_category_translation")
    return issues

def check_q2_sql(sql):
    """Must use SUM(order_revenue) per state, not AVG"""
    issues = []
    if "AVG(order_revenue)" in sql and "SUM(order_revenue)" not in sql:
        issues.append("Using AVG(order_revenue) → will produce 0 results; use SUM")
    if "olist_order_reviews" in sql and "AVG(order_revenue)" in sql:
        issues.append("Computing order_revenue avg from review-joined table → skewed")
    return issues

def check_q3_sql(sql):
    """MoM formula, no window fn in WHERE"""
    issues = []
    if "WHERE" in sql and "LAG(" in sql and "LAG(" in sql.split("WHERE")[1]:
        issues.append("Window function in WHERE clause")
    if re.search(r'total_revenue\s*-\s*LAG.*\*\s*1\.0\s*/\s*LAG', sql):
        issues.append("MoM formula operator precedence bug")
    return issues

def check_q4_sql(sql):
    """tat_risk must be ORDER BY ASC, review_risk must not be (1-x)"""
    issues = []
    if re.search(r'ORDER BY.*avg_tat.*DESC.*AS tat_risk', sql, re.IGNORECASE):
        issues.append("tat_risk uses DESC — should be ASC (longer TAT = rank 1)")
    if re.search(r'1\s*-\s*review_risk', sql):
        issues.append("(1 - review_risk) inverts correct review_risk direction")
    if "WHERE order_status = 'delivered'" in sql and "cancel_rate" in sql:
        issues.append("Status filter before cancel_rate → cancel_rate will be 0")
    return issues

def check_q5_sql(sql):
    """GROUP BY grp only, not by avg_review_score"""
    issues = []
    if "GROUP BY" in sql and "avg_review_score" in sql.split("GROUP BY")[-1]:
        issues.append("GROUP BY includes avg_review_score → 1 row per seller not per group")
    if "PERCENTILE_CONT" in sql:
        issues.append("PERCENTILE_CONT used — should be cumulative SUM OVER")
    return issues

SQL_CHECKS = [check_q1_sql, check_q2_sql, check_q3_sql, check_q4_sql, check_q5_sql]

# ── Import pipeline ───────────────────────────────────────────────────────────
import app.eval.cache as _cm; import app.rag.retriever as _rm
from app.llm.sql_generator import generate_sql
from app.sql.guardrails import validate_sql, enforce_limit
from app.slack.handler import summarise_results

def run_once(run_num):
    _cm._client=None; _cm._collection=None; _rm._client=None; _rm._collection=None
    print(f"\n{'='*60}")
    print(f"RUN {run_num}")
    print(f"{'='*60}")
    results = []
    for i, q in enumerate(TEST_QUESTIONS, 1):
        t0 = time.time()
        sql = ""
        try:
            sql = generate_sql(q)
            # Logic check
            logic_issues = SQL_CHECKS[i-1](sql)
            is_valid, reason, _ = validate_sql(sql)
            if not is_valid:
                latency = round(time.time()-t0,1)
                print(f"  Q{i} GUARDRAIL ({latency}s): {reason}")
                results.append({"q":i,"status":"GUARDRAIL","reason":reason})
                continue
            sql = enforce_limit(sql)
            rows = run_query(sql)
            latency = round(time.time()-t0,1)
            status = "PASS" if rows is not None and not logic_issues else "LOGIC_WARN"
            issues_str = "; ".join(logic_issues) if logic_issues else "none"
            print(f"  Q{i} {status} ({len(rows)} rows, {latency}s) logic={issues_str}")
            # Print key SQL excerpt
            excerpt = sql[:300].replace('\n',' ')
            print(f"     SQL: {excerpt}...")
            results.append({"q":i,"status":status,"rows":len(rows),"logic":logic_issues,"latency":latency})
        except Exception as e:
            latency = round(time.time()-t0,1)
            tb = traceback.format_exc().strip().split('\n')[-1]
            print(f"  Q{i} ERROR ({latency}s): {tb}")
            print(f"     SQL: {sql[:200]}...")
            results.append({"q":i,"status":"ERROR","reason":str(e)[:100]})
    return results

all_runs = []
for r in range(1, 4):
    all_runs.append(run_once(r))
    if r < 3:
        print("\n[Sleeping 5s between runs to avoid rate limiting]")
        time.sleep(5)

print(f"\n{'='*60}")
print("SUMMARY ACROSS 3 RUNS")
print(f"{'='*60}")
for run_i, run in enumerate(all_runs, 1):
    passed = sum(1 for r in run if r["status"] in ("PASS","LOGIC_WARN"))
    logic_warns = [r for r in run if r.get("logic")]
    print(f"Run {run_i}: {passed}/5 pass  logic_warnings={[f'Q{r[\"q\"]}:{r[\"logic\"]}' for r in logic_warns]}")
total_pass = sum(1 for run in all_runs for r in run if r["status"] == "PASS")
print(f"\nClean passes (no logic warnings): {total_pass}/15")
