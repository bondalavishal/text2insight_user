"""
Phase 8 — Eval Logger (local CSV)
Mirrors the Databricks schema so both logs stay in sync.

status values : success | cache_hit | blocked | disallowed_source | failed
interaction_type: data_query | greeting | out_of_scope | stats | download
latency_ms    : replaces latency_sec (millisecond precision)
"""

import os
import csv
from datetime import datetime

LOG_DIR  = os.path.join(os.path.dirname(__file__))
LOG_FILE = os.path.join(LOG_DIR, "eval_log.csv")

FIELDNAMES = [
    "timestamp",
    "interaction_type",
    "question",
    "sql",
    "rows_returned",
    "latency_ms",
    "cached",
    "status",           # success | cache_hit | blocked | disallowed_source | failed
    "anomaly_count",
    "failure_reason",
]


def _ensure_file():
    """Create CSV with headers if it doesn't exist."""
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()


def log(
    question:         str,
    sql:              str   = "",
    rows_returned:    int   = 0,
    latency_ms:       int   = 0,
    cached:           bool  = False,
    status:           str   = "success",
    anomaly_count:    int   = 0,
    failure_reason:   str   = "",
    interaction_type: str   = "data_query",
) -> None:
    """Append one row to the local eval log CSV."""
    try:
        _ensure_file()
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writerow({
                "timestamp":        datetime.now().isoformat(),
                "interaction_type": interaction_type,
                "question":         question,
                "sql":              sql.replace("\n", " "),
                "rows_returned":    rows_returned,
                "latency_ms":       int(latency_ms),
                "cached":           cached,
                "status":           status,
                "anomaly_count":    anomaly_count,
                "failure_reason":   failure_reason,
            })
    except Exception as e:
        print(f"[Logger] Failed to log: {e}")


def get_stats() -> dict:
    """
    Read the CSV and return summary stats.
    Used by the `stats` Slack command.
    """
    try:
        _ensure_file()
        rows = []
        with open(LOG_FILE, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        if not rows:
            return {"total": 0}

        # Only count data_query rows for pass/fail metrics
        data_rows  = [r for r in rows if r.get("interaction_type") == "data_query"]
        total      = len(data_rows)

        if total == 0:
            return {"total": 0}

        passed            = sum(1 for r in data_rows if r["status"] == "success")
        failed            = sum(1 for r in data_rows if r["status"] == "failed")
        blocked           = sum(1 for r in data_rows if r["status"] == "blocked")
        disallowed        = sum(1 for r in data_rows if r["status"] == "disallowed_source")
        cache_hits        = sum(1 for r in data_rows if r["status"] == "cache_hit")
        anomaly_count     = sum(int(r.get("anomaly_count", 0)) for r in data_rows)

        live_latencies = [
            int(r["latency_ms"]) for r in data_rows
            if r.get("latency_ms") and r["status"] not in ("cache_hit",)
        ]
        avg_latency_ms = round(sum(live_latencies) / len(live_latencies)) if live_latencies else 0

        cache_latencies = [
            int(r["latency_ms"]) for r in data_rows
            if r["status"] == "cache_hit" and r.get("latency_ms")
        ]
        avg_cache_latency_ms = round(sum(cache_latencies) / len(cache_latencies)) if cache_latencies else 0

        return {
            "total":                  total,
            "pass_rate":              f"{round(passed / total * 100, 1)}%",
            "passed":                 passed,
            "failed":                 failed,
            "blocked":                blocked,
            "disallowed_source":      disallowed,
            "cache_hits":             cache_hits,
            "cache_hit_rate":         f"{round(cache_hits / total * 100, 1)}%",
            "total_anomalies":        anomaly_count,
            "avg_latency_ms":         avg_latency_ms,
            "avg_cache_latency_ms":   avg_cache_latency_ms,
        }
    except Exception as e:
        print(f"[Logger] Failed to get stats: {e}")
        return {"total": 0, "error": str(e)}
