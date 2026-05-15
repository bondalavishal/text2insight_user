"""
chart_generator.py — LLM-backed viz classification + chart rendering.

Entry points:
    classify_viz(question, rows)          → VizSpec | None   (LLM + heuristic fallback)
    generate_chart(question, rows, spec)  → bytes  | None   (PNG)

Supported chart types:
    line         time-series (year+month axis)
    multi_line   multiple series over time (one line per category/state/year)
    hbar         horizontal bar — rankings / top-N
    bar          vertical bar — small categorical comparisons
    grouped_bar  multiple metrics side-by-side per category
    stacked_bar  composition breakdown (value split by a sub-group column)
    pie          proportions / share / distribution (≤ 8 slices; rest → "Other")
    scatter      correlation between two numeric variables

Thread-safe: uses OOP matplotlib Figure API (never pyplot).
"""

import io
import re
import os
import json
import logging
import functools
import httpx
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from dataclasses import dataclass
from typing import Optional
from app.utils import quiet_macos

# Silence Python-level matplotlib chatter
logging.getLogger("matplotlib").setLevel(logging.ERROR)
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)


def _silent_render(fn):
    """Decorator: wraps a chart renderer in quiet_macos() — no indentation changes needed."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with quiet_macos():
            return fn(*args, **kwargs)
    return _wrapper

# ── Style constants ───────────────────────────────────────────────────────────
PALETTE    = ["#4C72B0", "#DD8452", "#55A868", "#C44E52",
              "#8172B3", "#937860", "#DA8BC3", "#8C8C8C",
              "#BCB23A", "#17BECF"]
BG         = "#FAFAFA"
GRID_COLOR = "#E8E8E8"
TEXT_COLOR = "#333333"
SUB_COLOR  = "#666666"

_GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
_VIZ_TIMEOUT = 8                            # seconds — Groq / OpenRouter timeout


# ── Viz specification ─────────────────────────────────────────────────────────
@dataclass
class VizSpec:
    chart_type: str               # see module docstring for valid values
    label_col:  str               # x-axis labels / pie slice names / scatter annotation
    value_cols: list              # numeric cols to plot (2 cols for scatter: [x, y])
    series_col: Optional[str] = None  # multi_line / stacked_bar series grouping col
    year_col:   Optional[str] = None
    month_col:  Optional[str] = None
    title:      Optional[str] = None  # clean executive-friendly title from LLM


# ── Helpers ───────────────────────────────────────────────────────────────────
def _is_numeric(rows: list, col: str) -> bool:
    for row in rows:
        v = row.get(col)
        if v is None:
            continue
        try:
            float(v)
            return True
        except (TypeError, ValueError):
            return False
    return False


def _clean_label(col: str) -> str:
    """Clean a column name for use as an axis/legend label."""
    return col.replace("_", " ").title()


def _clean_value(v: str) -> str:
    """Clean a data value for use as a chart label (pie slice, legend entry, tick).
    Replaces underscores with spaces and applies title case.
    e.g. 'credit_card' → 'Credit Card', 'under_30_days' → 'Under 30 Days'
    """
    return str(v).replace("_", " ").title()


def _fmt_number(x, _pos=None) -> str:
    if x is None:
        return ""
    ax = abs(x)
    if ax >= 1_000_000:
        return f"{x/1_000_000:.1f}M"
    if ax >= 1_000:
        return f"{x/1_000:.0f}K"
    if ax == 0:
        return "0"
    return f"{x:.1f}" if x != int(x) else str(int(x))


def _chart_title(spec: "VizSpec", question: str) -> str:
    """Return the best available chart title: LLM-generated → question fallback."""
    if spec.title:
        t = spec.title.strip()
        return t if len(t) <= 80 else t[:77] + "..."
    # fallback: strip batch prefix and truncate at a word boundary
    t = re.sub(r"^\d+[\.)]\s*", "", question.strip())
    if len(t) <= 72:
        return t
    # truncate to last complete word within 72 chars
    truncated = t[:72]
    last_space = truncated.rfind(" ")
    return (truncated[:last_space] if last_space > 40 else truncated) + "…"


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ── LLM viz classifier — provider config ─────────────────────────────────────
_CEREBRAS_URL  = "https://api.cerebras.ai/v1/chat/completions"
_CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"
_CEREBRAS_TIMEOUT = 4          # viz is tiny — cerebras should respond in < 1s

_GROQ_VIZ_MODELS = [
    "llama-3.1-8b-instant",    # fastest, primary
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
]

_OPENROUTER_VIZ_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

_OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
_OLLAMA_MODEL = "mannix/defog-llama3-sqlcoder-8b"

# ── LLM viz classifier ────────────────────────────────────────────────────────
_VIZ_PROMPT = """\
You are a data visualisation classifier. Given a business question and its SQL result columns, \
choose the best chart type and map columns to chart roles.

Question: {question}
Columns: {cols}
Row count: {row_count}
Sample (first 3 rows): {sample}

Chart types:
  line         — time series; x = single date/month-year col, y = numeric
  multi_line   — multiple series over time; x = time col, y = numeric, series = categorical grouping col
  hbar         — horizontal bar ranking (top-N, best/worst); label on y-axis
  bar          — vertical bar; few categories (≤ 12)
  grouped_bar  — 2–4 numeric metrics side-by-side per category (≤ 15 rows)
  stacked_bar  — composition breakdown; category on x, stacked by a sub-group col
  pie          — proportions / share / "how X breaks down"; ≤ 8 meaningful slices
  scatter      — correlation / relationship between exactly 2 numeric vars
  none         — single-row result, free text, or genuinely not chartable

Rules:
- label_col   : categorical column for x-axis labels or pie slice names (must not be purely numeric)
- value_cols  : list of numeric column names to plot
                scatter → exactly [x_col, y_col]
                pie / line / hbar / bar / stacked_bar → exactly [one numeric col]
                grouped_bar / multi_line → one numeric col in list
- series_col  : only for multi_line or stacked_bar — the column defining separate series/layers; null otherwise
- pie vs stacked_bar: use PIE only when there is 1 categorical col + 1 numeric col and the categories are the slices of a whole (row count ≤ 8). Use STACKED_BAR when the data has 3 columns: a primary category (x-axis), a sub-group column (stacking dimension), and a numeric value — even if the question says "breakdown". Key signal: if the question says "for each [X], breakdown/split by [Y]" or if there are two categorical columns in the data, prefer stacked_bar with series_col = the sub-group column
- multi_line vs line: use MULTI_LINE when there is a time column AND a separate categorical column that defines different series (e.g. year, state, category). Set series_col to that categorical column and label_col to the time column. Use LINE only for a single series over time
- Prefer scatter when the question asks for correlation, relationship, or compares two metrics per entity
- If row count > 20 and chart is bar/hbar, hbar is almost always better
- title: a concise, executive-friendly chart title (max 8 words, noun phrase, no trailing question mark). Examples: "Revenue Concentration by Seller Tier", "Payment Mix — Top 8 States", "Monthly GMV Trend 2016–2018"
- Return ONLY valid JSON, no explanation, no markdown:
{{"chart_type":"hbar","label_col":"seller_id","value_cols":["total_revenue"],"series_col":null,"title":"Top 10 Sellers by Revenue"}}"""


def _call_llm_classifier(question: str, rows: list) -> Optional[VizSpec]:
    """
    Calls LLM to classify the viz type.
    4-stage fallback: Cerebras → Groq (3 models) → OpenRouter (3 models) → Ollama.
    Returns VizSpec or None (caller falls back to heuristic).
    """
    cols   = list(rows[0].keys())
    sample = json.dumps(rows[:3], default=str)[:600]
    prompt = _VIZ_PROMPT.format(
        question=question[:300],
        cols=cols,
        row_count=len(rows),
        sample=sample,
    )
    messages = [{"role": "user", "content": prompt}]
    raw: Optional[str] = None

    # ── 1. Cerebras ───────────────────────────────────────────────────────────
    cerebras_key = os.getenv("CEREBRAS_API_KEY")
    if cerebras_key:
        try:
            r = httpx.post(
                _CEREBRAS_URL,
                headers={"Authorization": f"Bearer {cerebras_key}",
                         "Content-Type": "application/json"},
                json={"model": _CEREBRAS_MODEL, "messages": messages,
                      "temperature": 0, "max_tokens": 120},
                timeout=_CEREBRAS_TIMEOUT,
            )
            if r.status_code not in (429, 503):
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"].strip()
                print("[Chart] Viz classified via Cerebras")
        except Exception as e:
            print(f"[Chart] Cerebras viz failed ({e}) — trying Groq")

    # ── 2. Groq (rotate models on 429) ───────────────────────────────────────
    if raw is None:
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            for model in _GROQ_VIZ_MODELS:
                try:
                    r = httpx.post(
                        _GROQ_URL,
                        headers={"Authorization": f"Bearer {groq_key}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages,
                              "temperature": 0, "max_tokens": 120},
                        timeout=_VIZ_TIMEOUT,
                    )
                    if r.status_code == 429:
                        print(f"[Chart] Groq {model} rate-limited — next model")
                        continue
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    print(f"[Chart] Viz classified via Groq ({model})")
                    break
                except Exception as e:
                    print(f"[Chart] Groq {model} failed ({e}) — next model")

    # ── 3. OpenRouter (rotate models on 429) ─────────────────────────────────
    if raw is None:
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if openrouter_key:
            for model in _OPENROUTER_VIZ_MODELS:
                try:
                    r = httpx.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {openrouter_key}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages,
                              "temperature": 0, "max_tokens": 120},
                        timeout=_VIZ_TIMEOUT,
                    )
                    if r.status_code == 429:
                        print(f"[Chart] OpenRouter {model} rate-limited — next model")
                        continue
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    print(f"[Chart] Viz classified via OpenRouter ({model})")
                    break
                except Exception as e:
                    print(f"[Chart] OpenRouter {model} failed ({e}) — next model")

    # ── 4. Ollama (last resort) ───────────────────────────────────────────────
    if raw is None:
        try:
            r = httpx.post(
                _OLLAMA_URL,
                json={"model": _OLLAMA_MODEL, "prompt": prompt,
                      "stream": False, "options": {"temperature": 0, "num_predict": 120}},
                timeout=60,
            )
            raw = r.json().get("response", "").strip()
            print("[Chart] Viz classified via Ollama (last resort)")
        except Exception as e:
            print(f"[Chart] Ollama viz also failed ({e}) — using heuristic")

    if not raw:
        return None

    try:
        # extract the JSON object even if the model wraps it in backticks
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        spec_dict  = json.loads(m.group())
        chart_type = spec_dict.get("chart_type", "none")
        if chart_type == "none":
            return None

        label_col  = spec_dict.get("label_col", "")
        value_cols = spec_dict.get("value_cols") or []
        series_col = spec_dict.get("series_col") or None
        llm_title  = spec_dict.get("title") or None

        # validate every column the LLM returned actually exists
        if label_col not in cols:
            # try to recover — pick first non-numeric col
            label_col = next((c for c in cols if not _is_numeric(rows, c)), cols[0])
        value_cols = [vc for vc in value_cols if vc in cols and _is_numeric(rows, vc)]
        if not value_cols:
            return None
        if series_col and series_col not in cols:
            series_col = None

        year_col  = next((c for c in cols if re.search(r"year",  c, re.I)), None)
        month_col = next((c for c in cols if re.search(r"month", c, re.I)), None)

        # ── Post-processing corrections ───────────────────────────────────
        # "bar" with multiple value cols → grouped_bar
        if chart_type == "bar" and len(value_cols) > 1:
            chart_type = "grouped_bar"

        # "bar" with series_col → stacked_bar
        if chart_type == "bar" and series_col:
            chart_type = "stacked_bar"

        # Data-shape override: 2 categorical + 1 numeric = stacked_bar
        # The LLM may classify this as "bar" without a series_col when the
        # question uses "breakdown by" or "per X split by Y" phrasing.
        if chart_type in ("bar", "hbar") and not series_col:
            non_numeric = [c for c in cols if not _is_numeric(rows, c)]
            if len(non_numeric) == 2 and len(value_cols) == 1:
                # first non-numeric → x-axis label, second → series grouping
                chart_type = "stacked_bar"
                label_col  = non_numeric[0]
                series_col = non_numeric[1]

        # for line/multi_line with year+month and no series → merge to _combined
        if chart_type in ("line", "multi_line") and year_col and month_col and not series_col:
            label_col = "_combined"
            chart_type = "line"

        print(f"[Chart] LLM classified → {chart_type}  label={label_col}  "
              f"values={value_cols}  series={series_col}  title={llm_title!r}")
        return VizSpec(
            chart_type=chart_type,
            label_col=label_col,
            value_cols=value_cols,
            series_col=series_col,
            year_col=year_col,
            month_col=month_col,
            title=llm_title,
        )

    except Exception as e:
        print(f"[Chart] LLM classifier failed ({e}) — falling back to heuristic")
        return None


# ── Heuristic fallback ────────────────────────────────────────────────────────
def should_chart(rows: list) -> Optional[VizSpec]:
    """
    Fast heuristic — no LLM call.
    Used as fallback when classify_viz's LLM call fails.
    """
    if not rows or len(rows) < 2:
        return None

    cols = list(rows[0].keys())
    if len(cols) < 2 or len(cols) > 7:
        return None

    year_col  = next((c for c in cols if re.search(r"year",              c, re.I)), None)
    month_col = next((c for c in cols if re.search(r"month",             c, re.I)), None)
    date_col  = next((c for c in cols if re.search(r"date|period|quarter", c, re.I)), None)
    time_axis_cols = {c for c in [year_col, month_col, date_col] if c}

    if year_col and month_col and len(rows) >= 3:
        value_cols = [c for c in cols if c not in time_axis_cols and _is_numeric(rows, c)]
        if value_cols:
            return VizSpec(chart_type="line", label_col="_combined",
                           value_cols=value_cols[:2], year_col=year_col, month_col=month_col)
    if date_col and len(rows) >= 3:
        value_cols = [c for c in cols if c not in time_axis_cols and _is_numeric(rows, c)]
        if value_cols:
            return VizSpec(chart_type="line", label_col=date_col, value_cols=value_cols[:2])

    numeric_cols = [c for c in cols if _is_numeric(rows, c)]
    label_cols   = [c for c in cols if c not in numeric_cols]
    if not numeric_cols or not label_cols:
        return None

    if len(label_cols) == 1 and len(numeric_cols) == 1 and 2 <= len(rows) <= 20:
        return VizSpec(chart_type="hbar", label_col=label_cols[0], value_cols=numeric_cols)
    if len(label_cols) == 1 and 2 <= len(numeric_cols) <= 4 and 2 <= len(rows) <= 15:
        return VizSpec(chart_type="grouped_bar", label_col=label_cols[0], value_cols=numeric_cols[:3])
    if len(label_cols) == 1 and len(numeric_cols) == 1 and 2 <= len(rows) <= 12:
        return VizSpec(chart_type="bar", label_col=label_cols[0], value_cols=numeric_cols)

    # multi-year categorical grid → filter to richest year in generate_chart
    if year_col and len(label_cols) == 1 and len(rows) <= 500:
        val_cols = [c for c in numeric_cols if c != year_col]
        if len(val_cols) == 1:
            return VizSpec(chart_type="hbar", label_col=label_cols[0],
                           value_cols=val_cols, year_col=year_col)
    return None


# ── Public entry point ────────────────────────────────────────────────────────
def classify_viz(question: str, rows: list) -> Optional[VizSpec]:
    """
    LLM-first viz classification with heuristic fallback.
    Returns a VizSpec (chart type + column mappings) or None if not chartable.
    """
    if not rows or len(rows) < 2:
        return None

    spec = _call_llm_classifier(question, rows)
    if spec is not None:
        return spec

    # LLM unavailable or failed — fall back to shape-based heuristic
    return should_chart(rows)


# ── Chart generator ───────────────────────────────────────────────────────────
@_silent_render
def generate_chart(question: str, rows: list, spec: VizSpec) -> Optional[bytes]:
    """
    Renders a PNG for the given VizSpec.
    Returns PNG bytes, or None on any error (caller falls back to text-only).
    Thread-safe: uses Figure() OOP API, never pyplot.
    """
    try:
        chart_type = spec.chart_type

        # ── PIE ──────────────────────────────────────────────────────────────
        if chart_type == "pie":
            labels = [_clean_value(r.get(spec.label_col, "")) for r in rows]
            vals   = [_safe_float(r.get(spec.value_cols[0])) for r in rows]

            # keep top-7 + merge rest into "Other"
            if len(labels) > 8:
                combined = sorted(zip(vals, labels), reverse=True)
                top      = combined[:7]
                other_v  = sum(v for v, _ in combined[7:])
                vals     = [v for v, _ in top] + [other_v]
                labels   = [l for _, l in top] + ["Other"]

            fig = Figure(figsize=(8, 7), facecolor=BG)
            ax  = fig.add_subplot(111)
            ax.set_facecolor(BG)

            wedges, texts, autotexts = ax.pie(
                vals, labels=None,
                autopct=lambda p: f"{p:.1f}%" if p >= 2 else "",
                colors=PALETTE[:len(vals)],
                startangle=90,
                pctdistance=0.78,
                wedgeprops={"linewidth": 0.8, "edgecolor": "white"},
            )
            for at in autotexts:
                at.set_fontsize(8)
                at.set_color("white")

            ax.legend(
                wedges, labels,
                loc="lower center", bbox_to_anchor=(0.5, -0.12),
                ncol=min(4, len(labels)), fontsize=8,
                framealpha=0, labelcolor=TEXT_COLOR,
            )
            ax.set_aspect("equal")
            ax.set_title(_chart_title(spec, question), fontsize=11, fontweight="bold",
                         pad=14, color=TEXT_COLOR)
            fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom",
                     fontsize=7, color="#BBBBBB", style="italic")
            fig.tight_layout(pad=1.5)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
            buf.seek(0)
            return buf.read()

        # ── SCATTER ──────────────────────────────────────────────────────────
        if chart_type == "scatter":
            if len(spec.value_cols) < 2:
                return None
            x_col, y_col = spec.value_cols[0], spec.value_cols[1]
            xs = [_safe_float(r.get(x_col)) for r in rows]
            ys = [_safe_float(r.get(y_col)) for r in rows]

            fig = Figure(figsize=(9, 6), facecolor=BG)
            ax  = fig.add_subplot(111)
            ax.set_facecolor(BG)
            for spine in ax.spines.values():
                spine.set_color(GRID_COLOR)
            ax.tick_params(colors=SUB_COLOR, labelsize=9)
            ax.grid(color=GRID_COLOR, linewidth=0.8, zorder=0)

            ax.scatter(xs, ys, color=PALETTE[0], alpha=0.65, s=45, zorder=3,
                       edgecolors="white", linewidths=0.4)
            ax.set_xlabel(_clean_label(x_col), fontsize=9, color=SUB_COLOR)
            ax.set_ylabel(_clean_label(y_col), fontsize=9, color=SUB_COLOR)
            ax.xaxis.set_major_formatter(FuncFormatter(_fmt_number))
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))
            ax.set_title(_chart_title(spec, question), fontsize=11, fontweight="bold",
                         pad=14, color=TEXT_COLOR)
            fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom",
                     fontsize=7, color="#BBBBBB", style="italic")
            fig.tight_layout(pad=1.5)

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
            buf.seek(0)
            return buf.read()

        # ── MULTI-LINE ───────────────────────────────────────────────────────
        if chart_type == "multi_line":
            val_col    = spec.value_cols[0]
            series_col = spec.series_col

            if not series_col or series_col not in rows[0]:
                # no series col — degrade to plain line
                chart_type = "line"
            else:
                # build x-axis: year+month or label_col
                def _x_label(r):
                    if spec.label_col == "_combined" and spec.year_col and spec.month_col:
                        return (f"{int(r.get(spec.year_col,0))}-"
                                f"{int(r.get(spec.month_col,0)):02d}")
                    return str(r.get(spec.label_col, ""))

                # stable ordered x-axis values
                x_vals    = list(dict.fromkeys(_x_label(r) for r in rows))
                # limit series to top 6 by total value
                series_totals: dict = {}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    series_totals[s] = series_totals.get(s, 0) + _safe_float(r.get(val_col))
                top_series = [s for s, _ in
                              sorted(series_totals.items(), key=lambda kv: kv[1], reverse=True)[:6]]

                # build data map  series → {x_label: value}
                data_map: dict = {s: {} for s in top_series}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    if s not in data_map:
                        continue
                    data_map[s][_x_label(r)] = _safe_float(r.get(val_col))

                fig = Figure(figsize=(12, 5), facecolor=BG)
                ax  = fig.add_subplot(111)
                ax.set_facecolor(BG)
                for spine in ax.spines.values():
                    spine.set_color(GRID_COLOR)
                ax.tick_params(colors=SUB_COLOR, labelsize=9)
                ax.grid(axis="y", color=GRID_COLOR, linewidth=0.8, zorder=0)

                for i, s in enumerate(top_series):
                    ys = [data_map[s].get(x, None) for x in x_vals]
                    ax.plot(x_vals, ys, marker="o", linewidth=2.0, markersize=4,
                            color=PALETTE[i % len(PALETTE)], label=_clean_value(s), zorder=3)

                ax.set_xticks(range(len(x_vals)))
                rot = 45 if len(x_vals) > 8 else 0
                ax.set_xticklabels(x_vals, rotation=rot,
                                   ha="right" if rot else "center", fontsize=8)
                ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))
                ax.legend(fontsize=8, framealpha=0.8, title=_clean_label(series_col),
                          title_fontsize=8)
                ax.set_title(_chart_title(spec, question), fontsize=11, fontweight="bold",
                             pad=14, color=TEXT_COLOR)
                fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom",
                         fontsize=7, color="#BBBBBB", style="italic")
                fig.tight_layout(pad=1.5)

                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
                buf.seek(0)
                return buf.read()

        # ── STACKED BAR ──────────────────────────────────────────────────────
        if chart_type == "stacked_bar":
            val_col    = spec.value_cols[0]
            series_col = spec.series_col

            if not series_col or series_col not in rows[0]:
                chart_type = "bar"   # degrade
            else:
                x_cats     = list(dict.fromkeys(_clean_value(r.get(spec.label_col, "")) for r in rows))[:15]
                # top-6 series by total
                s_totals: dict = {}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    s_totals[s] = s_totals.get(s, 0) + _safe_float(r.get(val_col))
                top_s = [s for s, _ in
                         sorted(s_totals.items(), key=lambda kv: kv[1], reverse=True)[:6]]

                # data map  series → {x_cat: value}  (keys are already cleaned)
                data_map = {s: {x: 0.0 for x in x_cats} for s in top_s}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    x = _clean_value(r.get(spec.label_col, ""))
                    if s in data_map and x in data_map[s]:
                        data_map[s][x] += _safe_float(r.get(val_col))

                fig = Figure(figsize=(12, 5), facecolor=BG)
                ax  = fig.add_subplot(111)
                ax.set_facecolor(BG)
                for spine in ax.spines.values():
                    spine.set_color(GRID_COLOR)
                ax.tick_params(colors=SUB_COLOR, labelsize=9)
                ax.grid(axis="y", color=GRID_COLOR, linewidth=0.8, zorder=0)

                x_pos   = list(range(len(x_cats)))
                bottoms = [0.0] * len(x_cats)
                for i, s in enumerate(top_s):
                    heights = [data_map[s][x] for x in x_cats]
                    ax.bar(x_pos, heights, bottom=bottoms,
                           color=PALETTE[i % len(PALETTE)], label=_clean_value(s),
                           width=0.65, zorder=3)
                    bottoms = [b + h for b, h in zip(bottoms, heights)]

                ax.set_xticks(x_pos)
                ax.set_xticklabels(x_cats, rotation=30, ha="right", fontsize=9)
                ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))
                ax.legend(fontsize=8, framealpha=0.8, title=_clean_label(series_col),
                          title_fontsize=8)
                ax.set_title(_chart_title(spec, question), fontsize=11, fontweight="bold",
                             pad=14, color=TEXT_COLOR)
                fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom",
                         fontsize=7, color="#BBBBBB", style="italic")
                fig.tight_layout(pad=1.5)

                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
                buf.seek(0)
                return buf.read()

        # ── LINE / MULTI-LINE fallthrough / BAR / HBAR / GROUPED BAR ─────────
        # (multi_line and stacked_bar degrade here if series_col missing)

        # multi-year hbar: filter rows to year with highest total
        _filtered_year: Optional[int] = None
        if (spec.year_col and chart_type == "hbar"
                and spec.label_col != "_combined"):
            try:
                val_col = spec.value_cols[0]
                year_totals: dict = {}
                for r in rows:
                    y = int(r.get(spec.year_col, 0))
                    year_totals[y] = year_totals.get(y, 0.0) + _safe_float(r.get(val_col))
                if year_totals:
                    _filtered_year = max(year_totals, key=lambda y: year_totals[y])
                    rows = [r for r in rows if int(r.get(spec.year_col, 0)) == _filtered_year]
                    rows = rows[:20]
            except Exception:
                pass

        # build x-labels
        if spec.label_col == "_combined" and spec.year_col and spec.month_col:
            x_labels = [
                f"{int(r.get(spec.year_col,0))}-{int(r.get(spec.month_col,0)):02d}"
                for r in rows
            ]
        else:
            # clean data values: replace underscores, apply title case
            x_labels = [_clean_value(r.get(spec.label_col, "")) for r in rows]

        # build values dict
        values: dict = {}
        for vc in spec.value_cols:
            values[vc] = [_safe_float(r.get(vc)) for r in rows]

        # figure height
        fig_h = max(4.5, min(7.0, len(x_labels) * 0.35 + 2.5)) if chart_type == "hbar" else 5.0
        fig   = Figure(figsize=(11, fig_h), facecolor=BG)
        ax    = fig.add_subplot(111)
        ax.set_facecolor(BG)
        for spine in ax.spines.values():
            spine.set_color(GRID_COLOR)
        ax.tick_params(colors=SUB_COLOR, labelsize=9)
        ax.grid(axis="y" if chart_type != "hbar" else "x",
                color=GRID_COLOR, linewidth=0.8, zorder=0)

        # ── LINE ─────────────────────────────────────────────────────────────
        if chart_type == "line":
            for i, vc in enumerate(spec.value_cols):
                ax.plot(x_labels, values[vc], marker="o", linewidth=2.2, markersize=5,
                        color=PALETTE[i], label=_clean_label(vc), zorder=3)
            if len(spec.value_cols) > 1:
                ax.legend(fontsize=9, framealpha=0.8)
            ax.set_xticks(range(len(x_labels)))
            ax.set_xticklabels(
                x_labels,
                rotation=45 if len(x_labels) > 10 else 0,
                ha="right" if len(x_labels) > 10 else "center",
                fontsize=8 if len(x_labels) > 10 else 9,
            )
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))

        # ── HBAR ─────────────────────────────────────────────────────────────
        elif chart_type == "hbar":
            vals  = values[spec.value_cols[0]]
            y_pos = list(range(len(x_labels)))
            ax.barh(y_pos, vals, color=PALETTE[0], zorder=3, height=0.65)
            ax.set_yticks(y_pos)
            ax.set_yticklabels(x_labels, fontsize=9)
            ax.invert_yaxis()
            year_suffix = f"  ({_filtered_year})" if _filtered_year else ""
            ax.set_xlabel(_clean_label(spec.value_cols[0]) + year_suffix,
                          fontsize=9, color=SUB_COLOR)
            ax.xaxis.set_major_formatter(FuncFormatter(_fmt_number))
            ax.grid(axis="x", color=GRID_COLOR, linewidth=0.8, zorder=0)
            ax.grid(axis="y", visible=False)
            max_val = max(vals) if vals else 1
            for i, v in enumerate(vals):
                ax.text(v + max_val * 0.01, i, _fmt_number(v),
                        va="center", fontsize=8, color=SUB_COLOR)

        # ── VERTICAL BAR ─────────────────────────────────────────────────────
        elif chart_type == "bar":
            x_pos = list(range(len(x_labels)))
            ax.bar(x_pos, values[spec.value_cols[0]], color=PALETTE[0], zorder=3, width=0.65)
            ax.set_xticks(x_pos)
            ax.set_xticklabels(x_labels, rotation=30, ha="right", fontsize=9)
            ax.set_ylabel(_clean_label(spec.value_cols[0]), fontsize=9, color=SUB_COLOR)
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))

        # ── GROUPED BAR ──────────────────────────────────────────────────────
        elif chart_type == "grouped_bar":
            n_groups  = len(x_labels)
            n_metrics = len(spec.value_cols)
            width     = 0.75 / n_metrics
            x_pos     = list(range(n_groups))

            # Scale-collapse guard: if the largest metric's max is 50× bigger
            # than the smallest metric's max, normalize each to 0–100% of its own max
            # so all metrics are visible (e.g. revenue in millions vs review score ~4.0).
            metric_maxes = {
                vc: max((abs(v) for v in values[vc]), default=1) or 1
                for vc in spec.value_cols
            }
            maxes_list  = sorted(metric_maxes.values())
            scale_ratio = (maxes_list[-1] / maxes_list[0]) if maxes_list[0] > 0 else 1
            normalize   = len(spec.value_cols) > 1 and scale_ratio > 50

            plot_values = (
                {vc: [v / metric_maxes[vc] * 100 for v in values[vc]]
                 for vc in spec.value_cols}
                if normalize else values
            )

            for i, vc in enumerate(spec.value_cols):
                offset = (i - n_metrics / 2 + 0.5) * width
                ax.bar([x + offset for x in x_pos], plot_values[vc],
                       width=width * 0.92, color=PALETTE[i],
                       label=_clean_label(vc), zorder=3)
            ax.set_xticks(x_pos)
            ax.set_xticklabels(x_labels, rotation=30, ha="right", fontsize=9)
            ax.legend(fontsize=9, framealpha=0.8)
            if normalize:
                ax.set_ylabel("% of metric max  (normalized)", fontsize=9, color=SUB_COLOR)
                ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
                # add a note inside the chart so the reader knows
                ax.text(0.01, 0.97, "⚠ metrics normalized — each bar = % of its own max",
                        transform=ax.transAxes, fontsize=7, color=SUB_COLOR,
                        va="top", ha="left", style="italic")
            else:
                ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))

        # ── Title & watermark ────────────────────────────────────────────────
        ax.set_title(_chart_title(spec, question), fontsize=11, fontweight="bold",
                     pad=14, color=TEXT_COLOR)
        fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom",
                 fontsize=7, color="#BBBBBB", style="italic")
        fig.tight_layout(pad=1.5)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        print(f"[Chart] Generation failed: {e}")
        return None

