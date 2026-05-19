"""
chart_generator.py — LLM-backed viz classification + chart rendering.

Entry points:
    classify_viz(question, rows)                       → VizSpec | None
    generate_chart(question, rows, spec,
                   use_codegen=True)                   → bytes   | None

Primary path  — code generation:
    LLM writes Python code choosing any library (plotly / seaborn / matplotlib).
    Code is exec'd in a controlled namespace; result_bytes must be set to PNG bytes.
    Provider chain: Cerebras → Groq → OpenRouter → Ollama.

Fallback path — spec renderer:
    Deterministic seaborn/matplotlib renderer driven by a VizSpec.
    Used when code-gen fails, and always for cache-hit regen (use_codegen=False).

Supported spec chart types (fallback):
    line, multi_line, hbar, bar, grouped_bar, stacked_bar, pie, scatter
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
import seaborn as sns
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter
from dataclasses import dataclass
from typing import Optional
from app.utils import quiet_macos

try:
    import plotly.express as _px
    import plotly.graph_objects as _go
    _HAS_PLOTLY = True
except ImportError:
    _px = _go = None
    _HAS_PLOTLY = False

logging.getLogger("matplotlib").setLevel(logging.ERROR)
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
logging.getLogger("seaborn").setLevel(logging.ERROR)

import datetime as _dt

def _coerce_rows(rows: list) -> list:
    """Convert date/datetime/Decimal values to strings so exec'd code can't produce un-serialisable Timestamps."""
    coerced = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, (_dt.datetime, _dt.date)):
                new_row[k] = str(v)
            else:
                new_row[k] = v
        coerced.append(new_row)
    return coerced


def _silent_render(fn):
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

sns.set_theme(
    style="whitegrid",
    palette=PALETTE,
    rc={
        "figure.facecolor":  BG,
        "axes.facecolor":    BG,
        "grid.color":        GRID_COLOR,
        "grid.linewidth":    0.8,
        "text.color":        TEXT_COLOR,
        "axes.labelcolor":   SUB_COLOR,
        "xtick.color":       SUB_COLOR,
        "ytick.color":       SUB_COLOR,
        "axes.edgecolor":    GRID_COLOR,
    },
)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


# ── Viz specification (used by fallback renderer + cache-hit regen) ───────────
@dataclass
class VizSpec:
    chart_type: str
    label_col:  str
    value_cols: list
    series_col: Optional[str] = None
    year_col:   Optional[str] = None
    month_col:  Optional[str] = None
    title:      Optional[str] = None


# ── Shared helpers ────────────────────────────────────────────────────────────
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
    return col.replace("_", " ").title()


def _clean_value(v) -> str:
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
    if spec.title:
        t = spec.title.strip()
        return t if len(t) <= 80 else t[:77] + "..."
    t = re.sub(r"^\d+[\.)]\s*", "", question.strip())
    if len(t) <= 72:
        return t
    truncated = t[:72]
    last_space = truncated.rfind(" ")
    return (truncated[:last_space] if last_space > 40 else truncated) + "…"


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _style_ax(ax) -> None:
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)
    ax.tick_params(colors=SUB_COLOR, labelsize=9)


def _finalize(fig: Figure, ax, spec: "VizSpec", question: str) -> None:
    ax.set_title(_chart_title(spec, question), fontsize=11, fontweight="bold",
                 pad=14, color=TEXT_COLOR)
    fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom",
             fontsize=7, color="#BBBBBB", style="italic")
    fig.tight_layout(pad=2.0)


def _savefig(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                pad_inches=0.2, facecolor=BG)
    buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════════════════════════════════════
# PRIMARY PATH — LLM code generation
# ═══════════════════════════════════════════════════════════════════════════════

_CEREBRAS_URL   = "https://api.cerebras.ai/v1/chat/completions"
_CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"

_CODEGEN_GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # best code quality on Groq
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
]

_CODEGEN_OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

_OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
_OLLAMA_MODEL = "mannix/defog-llama3-sqlcoder-8b"

_CODEGEN_PROMPT = """\
You are an expert Python data visualisation engineer. Write Python code that creates the best possible chart for this question and dataset.

Question: {question}

Data ({row_count} rows):
{data}

The following are already available in the execution scope — do NOT re-import them:
  io, pd (pandas), sns (seaborn), Figure (matplotlib.figure.Figure),
  FuncFormatter (matplotlib.ticker.FuncFormatter),
  px (plotly.express), go (plotly.graph_objects)

The data is available as `rows` — a Python list of dicts.

DATA PREPARATION — always do this before plotting:
  - Build a DataFrame first: df = pd.DataFrame(rows)
  - If separate integer `year` and `month` columns exist, combine them into a single time axis:
      df['period'] = df['year'].astype(str) + '-' + df['month'].astype(str).str.zfill(2)
    Use 'period' as the x-axis. NEVER use `year` alone as a color/series dimension when it is part of a time axis — that produces empty traces.
  - Drop rows where all primary metric columns are NaN: df = df.dropna(subset=[<metric_cols>])
  - For two different metrics over time (e.g. revenue + orders), use dual y-axes — never normalise or overlay on the same scale.
  - If df ends up empty after cleaning, set result_bytes = b"" and stop.

OUTPUT CONTRACT — your code MUST end by setting `result_bytes` to PNG bytes:
  # plotly  →  result_bytes = fig.to_image(format="png", width=1200, height=600, scale=1.5)
  # mpl/sns →  buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", pad_inches=0.2, facecolor="#FAFAFA"); buf.seek(0); result_bytes = buf.read()

LIBRARY CHOICE GUIDE — pick the best tool for the job:
  plotly   → treemap, sunburst, waterfall, funnel, sankey, choropleth, candlestick,
              box/violin with hover, heatmap with annotations, animated charts
  seaborn  → statistical distributions (violin, box, KDE, pairplot),
              correlation heatmap (sns.heatmap) — always pass ax= for thread safety
  matplotlib Figure() → anything else; NEVER use plt / pyplot (not thread-safe)

DATA SHAPE → CHART TYPE — read the data structure and choose accordingly, do NOT default to bar/line:
  - 2 categorical cols + 1 numeric  → plotly heatmap (matrix of values)
  - 1 category + signed +/- deltas that sum to a total → plotly waterfall
  - ordered pipeline stages with decreasing counts → plotly funnel
  - 3+ numeric cols per entity (e.g. revenue + delivery + score per state) → plotly bubble (scatter with size=)
  - 1 category + 1 large numeric, >10 rows, proportional area matters → plotly treemap
  - flow from one set of categories to another → plotly sankey
  - distribution of a metric across groups (spread matters, not just mean) → seaborn violin or box
  - 2 numeric cols, correlation or position matters → scatter (add trendline if correlated)
  - time series with one metric going up and down month-by-month → plotly waterfall, not line
  Only use bar/line when none of the above patterns fit.

STYLE RULES:
  - Background: #FAFAFA
  - Primary palette: {palette}
  - Text: #333333, axis labels / subtitles: #666666, grid: #E8E8E8
  - For mpl/sns: Figure(figsize=(12, 6), facecolor="#FAFAFA") — adjust as needed
  - For plotly: set template="plotly_white", paper_bgcolor="#FAFAFA", plot_bgcolor="#FAFAFA"
  - Title: "{title}"
  - Watermark: add "text2insight" bottom-right in #BBBBBB, 8pt italic
    mpl/sns → fig.text(0.99, 0.01, "text2insight", ha="right", va="bottom", fontsize=7, color="#BBBBBB", style="italic")
    plotly  → fig.add_annotation(text="text2insight", xref="paper", yref="paper", x=1, y=-0.07, showarrow=False, font=dict(size=8, color="#BBBBBB"), xanchor="right")
  - Clean axis labels: replace underscores with spaces, use title case

Write ONLY executable Python code. No markdown fences, no comments, no explanations.\
"""


def _call_codegen_llm(prompt: str) -> Optional[str]:
    """LLM chain for code generation. Returns raw code string or None."""
    messages = [{"role": "user", "content": prompt}]
    raw: Optional[str] = None

    # 1. Cerebras
    cerebras_key = os.getenv("CEREBRAS_API_KEY")
    if cerebras_key:
        try:
            r = httpx.post(
                _CEREBRAS_URL,
                headers={"Authorization": f"Bearer {cerebras_key}",
                         "Content-Type": "application/json"},
                json={"model": _CEREBRAS_MODEL, "messages": messages,
                      "temperature": 0, "max_tokens": 1500},
                timeout=20,
            )
            if r.status_code not in (429, 503):
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"].strip()
                print("[Chart] Code generated via Cerebras")
        except Exception as e:
            print(f"[Chart] Cerebras codegen failed ({e}) — trying Groq")

    # 2. Groq
    if raw is None:
        groq_key = os.getenv("GROQ_API_KEY")
        if groq_key:
            for model in _CODEGEN_GROQ_MODELS:
                try:
                    r = httpx.post(
                        _GROQ_URL,
                        headers={"Authorization": f"Bearer {groq_key}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages,
                              "temperature": 0, "max_tokens": 1500},
                        timeout=45,
                    )
                    if r.status_code in (429, 413):
                        print(f"[Chart] Groq {model} rate-limited — next model")
                        continue
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    print(f"[Chart] Code generated via Groq ({model})")
                    break
                except Exception as e:
                    print(f"[Chart] Groq {model} failed ({e}) — next model")

    # 3. OpenRouter
    if raw is None:
        or_key = os.getenv("OPENROUTER_API_KEY")
        if or_key:
            for model in _CODEGEN_OPENROUTER_MODELS:
                try:
                    r = httpx.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {or_key}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages,
                              "temperature": 0, "max_tokens": 1500},
                        timeout=60,
                    )
                    if r.status_code in (429, 413):
                        print(f"[Chart] OpenRouter {model} rate-limited — next model")
                        continue
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    print(f"[Chart] Code generated via OpenRouter ({model})")
                    break
                except Exception as e:
                    print(f"[Chart] OpenRouter {model} failed ({e}) — next model")

    # 4. Ollama
    if raw is None:
        try:
            r = httpx.post(
                _OLLAMA_URL,
                json={"model": _OLLAMA_MODEL, "prompt": prompt,
                      "stream": False, "options": {"temperature": 0, "num_predict": 1500}},
                timeout=120,
            )
            raw = r.json().get("response", "").strip()
            print("[Chart] Code generated via Ollama (last resort)")
        except Exception as e:
            print(f"[Chart] Ollama codegen failed ({e})")

    return raw


def _codegen_chart(question: str, rows: list) -> Optional[bytes]:
    """
    Primary chart path: ask LLM to write Python code, execute it, return PNG.
    Returns None if LLM call fails or generated code errors out.
    """
    title = re.sub(r"^\d+[\.)]\s*", "", question.strip())
    if len(title) > 80:
        title = title[:77] + "..."

    prompt = _CODEGEN_PROMPT.format(
        question=question,
        row_count=len(rows),
        data=json.dumps(rows, default=str),
        palette=PALETTE,
        title=title,
    )

    raw = _call_codegen_llm(prompt)
    if not raw:
        return None

    # strip markdown fences if the LLM wrapped the code anyway
    code = re.sub(r"^```(?:python)?\s*", "", raw, flags=re.MULTILINE)
    code = re.sub(r"```\s*$", "", code, flags=re.MULTILINE).strip()

    namespace: dict = {
        "io":             io,
        "pd":             pd,
        "sns":            sns,
        "Figure":         Figure,
        "FuncFormatter":  FuncFormatter,
        "px":             _px,
        "go":             _go,
        "rows":           _coerce_rows(rows),
        "result_bytes":   None,
    }

    try:
        with quiet_macos():
            exec(code, namespace)  # noqa: S102
        result = namespace.get("result_bytes")
        if isinstance(result, bytes) and len(result) > 25_000:
            print(f"[Chart] Code-gen chart OK ({len(result)//1024}KB)")
            return result
        if isinstance(result, bytes):
            print(f"[Chart] Code-gen chart too small ({len(result)} bytes) — likely blank, falling back")
        else:
            print("[Chart] Code-gen executed but result_bytes not set — falling back")
    except Exception as e:
        print(f"[Chart] Code-gen execution failed ({e}) — falling back to spec renderer")

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# FALLBACK PATH — LLM viz classifier + deterministic spec renderer
# ═══════════════════════════════════════════════════════════════════════════════

_CEREBRAS_TIMEOUT = 4
_VIZ_TIMEOUT      = 8

_GROQ_VIZ_MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
]

_OPENROUTER_VIZ_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

_VIZ_PROMPT = """\
You are a data visualisation classifier. Given a business question and its full SQL result, \
choose the best chart type and map columns to chart roles.

Question: {question}
Columns: {cols}
Row count: {row_count}
Full data: {data}

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
- pie vs stacked_bar: use PIE only when there is 1 categorical col + 1 numeric col and the categories are the slices of a whole (row count ≤ 8). Use STACKED_BAR when the data has 3 columns: a primary category (x-axis), a sub-group column (stacking dimension), and a numeric value.
- multi_line vs line: use MULTI_LINE when there is a time column AND a separate categorical column that defines different series. Set series_col to that categorical column and label_col to the time column. Use LINE only for a single series over time.
- Prefer scatter when the question asks for correlation, relationship, or compares two metrics per entity
- If row count > 20 and chart is bar/hbar, hbar is almost always better
- title: a concise, executive-friendly chart title (max 8 words, noun phrase, no trailing question mark)
- Return ONLY valid JSON, no explanation, no markdown:
{{"chart_type":"hbar","label_col":"seller_id","value_cols":["total_revenue"],"series_col":null,"title":"Top 10 Sellers by Revenue"}}"""


def _extract_first_json(text: str) -> Optional[dict]:
    """
    Extracts and parses the first balanced JSON object from text.
    Handles LLMs that return extra explanation after the JSON, or two JSON blocks.
    Returns parsed dict or None.
    """
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if escaped:
            escaped = False
            continue
        if ch == '\\' and in_str:
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _call_llm_classifier(question: str, rows: list) -> Optional[VizSpec]:
    if not rows:
        return None
    cols     = list(rows[0].keys())
    data     = json.dumps(rows, default=str)
    prompt   = _VIZ_PROMPT.format(question=question, cols=cols,
                                   row_count=len(rows), data=data)
    messages = [{"role": "user", "content": prompt}]
    raw: Optional[str] = None

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
                    if r.status_code in (429, 413):
                        continue
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    print(f"[Chart] Viz classified via Groq ({model})")
                    break
                except Exception as e:
                    print(f"[Chart] Groq {model} failed ({e}) — next model")

    if raw is None:
        or_key = os.getenv("OPENROUTER_API_KEY")
        if or_key:
            for model in _OPENROUTER_VIZ_MODELS:
                try:
                    r = httpx.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {or_key}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages,
                              "temperature": 0, "max_tokens": 120},
                        timeout=_VIZ_TIMEOUT,
                    )
                    if r.status_code in (429, 413):
                        continue
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
                    print(f"[Chart] Viz classified via OpenRouter ({model})")
                    break
                except Exception as e:
                    print(f"[Chart] OpenRouter {model} failed ({e}) — next model")

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
        spec_dict = _extract_first_json(raw)
        if spec_dict is None:
            return None
        chart_type = spec_dict.get("chart_type", "none")
        if chart_type == "none":
            return None

        label_col  = spec_dict.get("label_col", "")
        value_cols = spec_dict.get("value_cols") or []
        series_col = spec_dict.get("series_col") or None
        llm_title  = spec_dict.get("title") or None

        if label_col not in cols:
            label_col = next((c for c in cols if not _is_numeric(rows, c)), cols[0])
        value_cols = [vc for vc in value_cols if vc in cols and _is_numeric(rows, vc)]
        if not value_cols:
            return None
        if series_col and series_col not in cols:
            series_col = None

        year_col  = next((c for c in cols if re.search(r"year",  c, re.I)), None)
        month_col = next((c for c in cols if re.search(r"month", c, re.I)), None)

        if chart_type == "bar" and len(value_cols) > 1:
            chart_type = "grouped_bar"
        if chart_type == "bar" and series_col:
            chart_type = "stacked_bar"
        if chart_type in ("bar", "hbar") and not series_col:
            non_numeric = [c for c in cols if not _is_numeric(rows, c)]
            if len(non_numeric) == 2 and len(value_cols) == 1:
                chart_type = "stacked_bar"
                label_col  = non_numeric[0]
                series_col = non_numeric[1]
        if chart_type in ("line", "multi_line") and year_col and month_col and not series_col:
            label_col  = "_combined"
            chart_type = "line"

        print(f"[Chart] Spec fallback → {chart_type}  label={label_col}  "
              f"values={value_cols}  series={series_col}  title={llm_title!r}")
        return VizSpec(chart_type=chart_type, label_col=label_col, value_cols=value_cols,
                       series_col=series_col, year_col=year_col, month_col=month_col,
                       title=llm_title)

    except Exception as e:
        print(f"[Chart] LLM classifier failed ({e}) — using heuristic")
        return None


def should_chart(rows: list) -> Optional[VizSpec]:
    if not rows or len(rows) < 2:
        return None
    cols = list(rows[0].keys())
    if len(cols) < 2 or len(cols) > 7:
        return None

    year_col  = next((c for c in cols if re.search(r"year",              c, re.I)), None)
    month_col = next((c for c in cols if re.search(r"month",             c, re.I)), None)
    date_col  = next((c for c in cols if re.search(r"date|period|quarter|year.month|month.year", c, re.I)), None)
    time_axis_cols = {c for c in [year_col, month_col, date_col] if c}

    if year_col and month_col and year_col != month_col and len(rows) >= 3:
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
    if year_col and len(label_cols) == 1 and len(rows) <= 500:
        val_cols = [c for c in numeric_cols if c != year_col]
        if len(val_cols) == 1:
            return VizSpec(chart_type="hbar", label_col=label_cols[0],
                           value_cols=val_cols, year_col=year_col)
    return None


def classify_viz(question: str, rows: list) -> Optional[VizSpec]:
    """Returns a VizSpec for the fallback renderer (also used for cache-hit regen)."""
    if not rows or len(rows) < 2:
        return None
    spec = _call_llm_classifier(question, rows)
    return spec if spec is not None else should_chart(rows)


@_silent_render
def _spec_chart(question: str, rows: list, spec: VizSpec) -> Optional[bytes]:
    """
    Deterministic seaborn/matplotlib renderer driven by a VizSpec.
    Used as fallback when code-gen fails, and always for cache-hit regen.
    """
    try:
        chart_type = spec.chart_type

        # ── PIE ──────────────────────────────────────────────────────────────
        if chart_type == "pie":
            labels = [_clean_value(r.get(spec.label_col, "")) for r in rows]
            vals   = [_safe_float(r.get(spec.value_cols[0])) for r in rows]
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
                colors=PALETTE[:len(vals)], startangle=90, pctdistance=0.78,
                wedgeprops={"linewidth": 0.8, "edgecolor": "white"},
            )
            for at in autotexts:
                at.set_fontsize(8); at.set_color("white")
            ax.legend(wedges, labels, loc="lower center", bbox_to_anchor=(0.5, -0.12),
                      ncol=min(4, len(labels)), fontsize=8, framealpha=0, labelcolor=TEXT_COLOR)
            ax.set_aspect("equal")
            _finalize(fig, ax, spec, question)
            return _savefig(fig)

        # ── SCATTER ──────────────────────────────────────────────────────────
        if chart_type == "scatter":
            if len(spec.value_cols) < 2:
                return None
            x_col, y_col = spec.value_cols[0], spec.value_cols[1]
            df = pd.DataFrame(rows)
            df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
            df[y_col] = pd.to_numeric(df[y_col], errors="coerce")

            fig = Figure(figsize=(9, 6), facecolor=BG)
            ax  = fig.add_subplot(111)
            ax.set_facecolor(BG); _style_ax(ax)
            sns.scatterplot(data=df, x=x_col, y=y_col, ax=ax,
                            color=PALETTE[0], alpha=0.7, s=60,
                            edgecolor="white", linewidth=0.4, zorder=3)
            ax.set_xlabel(_clean_label(x_col), fontsize=9, color=SUB_COLOR)
            ax.set_ylabel(_clean_label(y_col), fontsize=9, color=SUB_COLOR)
            ax.xaxis.set_major_formatter(FuncFormatter(_fmt_number))
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))
            _finalize(fig, ax, spec, question)
            return _savefig(fig)

        # ── MULTI-LINE ───────────────────────────────────────────────────────
        if chart_type == "multi_line":
            val_col    = spec.value_cols[0]
            series_col = spec.series_col
            if not series_col or series_col not in rows[0]:
                chart_type = "line"
            else:
                def _x_label(r):
                    if spec.label_col == "_combined" and spec.year_col and spec.month_col:
                        if spec.year_col == spec.month_col:
                            return str(r.get(spec.year_col, ""))
                        return f"{int(r.get(spec.year_col,0))}-{int(r.get(spec.month_col,0)):02d}"
                    return str(r.get(spec.label_col, ""))

                series_totals: dict = {}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    series_totals[s] = series_totals.get(s, 0) + _safe_float(r.get(val_col))
                top_series = [s for s, _ in
                              sorted(series_totals.items(), key=lambda kv: kv[1], reverse=True)[:6]]

                df = pd.DataFrame(rows)
                df["_x"]    = df.apply(lambda r: _x_label(r), axis=1)
                df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
                df          = df[df[series_col].isin(top_series)].copy()
                df[series_col] = df[series_col].apply(_clean_value)
                x_order = list(dict.fromkeys(df["_x"]))

                fig = Figure(figsize=(12, 5), facecolor=BG)
                ax  = fig.add_subplot(111)
                ax.set_facecolor(BG); _style_ax(ax)
                # Sort df by x_order so lineplot renders in correct time sequence
                df["_x"] = pd.Categorical(df["_x"], categories=x_order, ordered=True)
                df = df.sort_values("_x")
                sns.lineplot(data=df, x="_x", y=val_col, hue=series_col,
                             marker="o", linewidth=2.0, markersize=5,
                             palette=PALETTE[:len(top_series)], ax=ax, zorder=3)
                rot = 45 if len(x_order) > 8 else 0
                ax.set_xticklabels(ax.get_xticklabels(),
                                   rotation=rot, ha="right" if rot else "center", fontsize=8)
                ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))
                ax.legend(fontsize=8, framealpha=0.8,
                          title=_clean_label(series_col), title_fontsize=8)
                ax.set_xlabel("")
                _finalize(fig, ax, spec, question)
                return _savefig(fig)

        # ── STACKED BAR ──────────────────────────────────────────────────────
        if chart_type == "stacked_bar":
            val_col    = spec.value_cols[0]
            series_col = spec.series_col
            if not series_col or series_col not in rows[0]:
                chart_type = "bar"
            else:
                x_cats = list(dict.fromkeys(
                    _clean_value(r.get(spec.label_col, "")) for r in rows))[:15]
                s_totals: dict = {}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    s_totals[s] = s_totals.get(s, 0) + _safe_float(r.get(val_col))
                top_s = [s for s, _ in
                         sorted(s_totals.items(), key=lambda kv: kv[1], reverse=True)[:6]]
                data_map = {s: {x: 0.0 for x in x_cats} for s in top_s}
                for r in rows:
                    s = str(r.get(series_col, ""))
                    x = _clean_value(r.get(spec.label_col, ""))
                    if s in data_map and x in data_map[s]:
                        data_map[s][x] += _safe_float(r.get(val_col))

                fig = Figure(figsize=(12, 5), facecolor=BG)
                ax  = fig.add_subplot(111)
                ax.set_facecolor(BG); _style_ax(ax)
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
                ax.legend(fontsize=8, framealpha=0.8,
                          title=_clean_label(series_col), title_fontsize=8)
                _finalize(fig, ax, spec, question)
                return _savefig(fig)

        # ── LINE / HBAR / BAR / GROUPED BAR ──────────────────────────────────
        _filtered_year: Optional[int] = None
        filtered_rows = rows  # default: use all rows unmodified
        if spec.year_col and chart_type == "hbar" and spec.label_col != "_combined":
            try:
                val_col = spec.value_cols[0]
                year_totals: dict = {}
                for r in rows:
                    y = int(r.get(spec.year_col, 0))
                    year_totals[y] = year_totals.get(y, 0.0) + _safe_float(r.get(val_col))
                if year_totals:
                    _filtered_year = max(year_totals, key=lambda y: year_totals[y])
                    filtered_rows = [r for r in rows if int(r.get(spec.year_col, 0)) == _filtered_year][:20]
            except Exception:
                pass
        rows = filtered_rows

        if spec.label_col == "_combined" and spec.year_col and spec.month_col:
            if spec.year_col == spec.month_col:
                x_labels = [str(r.get(spec.year_col, "")) for r in rows]
            else:
                x_labels = [f"{int(r.get(spec.year_col,0))}-{int(r.get(spec.month_col,0)):02d}"
                            for r in rows]
        else:
            x_labels = [_clean_value(r.get(spec.label_col, "")) for r in rows]

        values: dict = {vc: [_safe_float(r.get(vc)) for r in rows] for vc in spec.value_cols}

        fig_h = max(4.5, min(7.0, len(x_labels) * 0.35 + 2.5)) if chart_type == "hbar" else 5.0
        fig   = Figure(figsize=(11, fig_h), facecolor=BG)
        ax    = fig.add_subplot(111)
        ax.set_facecolor(BG); _style_ax(ax)

        if chart_type == "line":
            df_line = pd.DataFrame({"x": x_labels})
            for vc in spec.value_cols:
                df_line[vc] = values[vc]
            if len(spec.value_cols) == 1:
                sns.lineplot(data=df_line, x="x", y=spec.value_cols[0], ax=ax,
                             color=PALETTE[0], marker="o", linewidth=2.2, markersize=5, zorder=3)
            else:
                df_melted = df_line.melt(id_vars="x", value_vars=spec.value_cols,
                                         var_name="metric", value_name="value")
                df_melted["metric"] = df_melted["metric"].apply(_clean_label)
                sns.lineplot(data=df_melted, x="x", y="value", hue="metric", ax=ax,
                             palette=PALETTE[:len(spec.value_cols)],
                             marker="o", linewidth=2.2, markersize=5, zorder=3)
                ax.legend(fontsize=9, framealpha=0.8)
            ax.set_xticks(range(len(x_labels)))
            ax.set_xticklabels(x_labels,
                               rotation=45 if len(x_labels) > 10 else 0,
                               ha="right" if len(x_labels) > 10 else "center",
                               fontsize=8 if len(x_labels) > 10 else 9)
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))
            ax.set_xlabel("")

        elif chart_type == "hbar":
            vals    = values[spec.value_cols[0]]
            max_val = max(vals) if vals else 1
            df_hbar = pd.DataFrame({"label": x_labels, "value": vals})
            sns.barplot(data=df_hbar, x="value", y="label", ax=ax,
                        color=PALETTE[0], errorbar=None, orient="h", zorder=3)
            ax.invert_yaxis()
            ax.set_xlim(0, max_val * 1.18)
            year_suffix = f"  ({_filtered_year})" if _filtered_year else ""
            ax.set_xlabel(_clean_label(spec.value_cols[0]) + year_suffix,
                          fontsize=9, color=SUB_COLOR)
            ax.set_ylabel("")
            ax.xaxis.set_major_formatter(FuncFormatter(_fmt_number))
            ax.grid(axis="x", color=GRID_COLOR, linewidth=0.8, zorder=0)
            ax.grid(axis="y", visible=False)
            for i, v in enumerate(vals):
                ax.text(v + max_val * 0.01, i, _fmt_number(v),
                        va="center", fontsize=8, color=SUB_COLOR)

        elif chart_type == "bar":
            df_bar = pd.DataFrame({"label": x_labels, "value": values[spec.value_cols[0]]})
            sns.barplot(data=df_bar, x="label", y="value", ax=ax,
                        color=PALETTE[0], errorbar=None, zorder=3)
            ax.set_xticklabels(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9)
            ax.set_ylabel(_clean_label(spec.value_cols[0]), fontsize=9, color=SUB_COLOR)
            ax.set_xlabel("")
            ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))

        elif chart_type == "grouped_bar":
            n_metrics    = len(spec.value_cols)
            metric_maxes = {vc: max((abs(v) for v in values[vc]), default=1) or 1
                            for vc in spec.value_cols}
            maxes_list   = sorted(metric_maxes.values())
            scale_ratio  = (maxes_list[-1] / maxes_list[0]) if maxes_list[0] > 0 else 1
            normalize    = n_metrics > 1 and scale_ratio > 50
            plot_values  = ({vc: [v / metric_maxes[vc] * 100 for v in values[vc]]
                             for vc in spec.value_cols} if normalize else values)

            df_grp      = pd.DataFrame({"label": x_labels})
            metric_cols = []
            for vc in spec.value_cols:
                lbl = _clean_label(vc)
                df_grp[lbl] = plot_values[vc]
                metric_cols.append(lbl)
            df_melted = df_grp.melt(id_vars="label", value_vars=metric_cols,
                                    var_name="metric", value_name="value")
            sns.barplot(data=df_melted, x="label", y="value", hue="metric", ax=ax,
                        palette=PALETTE[:n_metrics], errorbar=None, zorder=3)
            ax.set_xticklabels(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9)
            ax.set_xlabel("")
            ax.legend(fontsize=9, framealpha=0.8)
            if normalize:
                ax.set_ylabel("% of metric max  (normalized)", fontsize=9, color=SUB_COLOR)
                ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
                ax.text(0.01, 0.97, "⚠ metrics normalized — each bar = % of its own max",
                        transform=ax.transAxes, fontsize=7, color=SUB_COLOR,
                        va="top", ha="left", style="italic")
            else:
                ax.yaxis.set_major_formatter(FuncFormatter(_fmt_number))

        _finalize(fig, ax, spec, question)
        return _savefig(fig)

    except Exception as e:
        print(f"[Chart] Spec renderer failed: {e}")
        return None


# ── Public entry point ────────────────────────────────────────────────────────
def generate_chart(question: str, rows: list, spec: VizSpec,
                   use_codegen: bool = True) -> Optional[bytes]:
    """
    Renders a PNG chart.
    use_codegen=True  (default) — try LLM code-gen first, fall back to spec renderer.
    use_codegen=False           — spec renderer only (used for cache-hit regen).
    """
    if use_codegen:
        png = _codegen_chart(question, rows)
        if png:
            return png
        print("[Chart] Code-gen failed — falling back to spec renderer")
    return _spec_chart(question, rows, spec)
