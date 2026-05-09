"""
spellcheck.py — Phase 9 addition
Corrects spelling, expands MBA/corporate abbreviations, and handles casual
shorthand in user prompts before they enter the main pipeline.

Processing order:
  1. Abbreviation expansion  — "YoY" → "year over year"
  2. Symbol substitution     — "&" → "and", "%" → "percent"
  3. pyspellchecker          — typo correction via edit-distance + word freq

No API calls — runs fully offline and never blocks the pipeline.

Usage:
    from app.llm.spellcheck import correct_prompt
    clean_text = correct_prompt(raw_text)
"""

import re
from datetime import datetime
from spellchecker import SpellChecker

_ts = lambda: datetime.now().strftime("%H:%M:%S")

_spell = SpellChecker()

# ── MBA / Corporate abbreviations ─────────────────────────────────────────────
# Expanded BEFORE pyspellchecker so short-form tokens aren't mangled.
# Keys are lowercase; matching is case-insensitive with word boundaries.
_ABBREVIATIONS = {
    # ── Temporal comparisons ─────────────────────────────────────────────────
    "yoy":   "year over year",
    "mom":   "month over month",
    "qoq":   "quarter over quarter",
    "wow":   "week over week",
    "ytd":   "year to date",
    "mtd":   "month to date",
    "qtd":   "quarter to date",
    "h1":    "first half",
    "h2":    "second half",
    "fy":    "fiscal year",
    "ltm":   "last twelve months",
    "ttm":   "trailing twelve months",
    "lm":    "last month",
    "lq":    "last quarter",
    "ly":    "last year",
    # ── Revenue / Finance ─────────────────────────────────────────────────────
    "aov":   "average order value",
    "atv":   "average transaction value",
    "asp":   "average selling price",
    "gmv":   "gross merchandise value",
    "arr":   "annual recurring revenue",
    "mrr":   "monthly recurring revenue",
    "ltv":   "lifetime value",
    "clv":   "customer lifetime value",
    "cac":   "customer acquisition cost",
    "roas":  "return on ad spend",
    "roi":   "return on investment",
    "cogs":  "cost of goods sold",
    "gm":    "gross margin",
    "ebitda":"earnings before interest taxes depreciation and amortization",
    # ── Delivery / Operations ─────────────────────────────────────────────────
    "otd":   "on time delivery",
    "otif":  "on time in full",
    "tat":   "turnaround time",
    "sla":   "service level agreement",
    "eta":   "estimated time of arrival",
    "etd":   "estimated time of delivery",
    "edd":   "estimated delivery date",
    "sdd":   "same day delivery",
    "ndd":   "next day delivery",
    # ── Customer / Satisfaction ───────────────────────────────────────────────
    "nps":   "net promoter score",
    "csat":  "customer satisfaction score",
    "ces":   "customer effort score",
    "fcr":   "first contact resolution",
    "churn": "churn",
    # ── E-commerce ───────────────────────────────────────────────────────────
    "sku":   "stock keeping unit",
    "skus":  "stock keeping units",
    "upt":   "units per transaction",
    "rfm":   "recency frequency monetary",
    "cart":  "shopping cart",
    "cvr":   "conversion rate",
    "cr":    "conversion rate",
    "ctr":   "click through rate",
    "aur":   "average unit retail",
    # ── General business ─────────────────────────────────────────────────────
    "kpi":   "key performance indicator",
    "kpis":  "key performance indicators",
    "okr":   "objective and key result",
    "b2b":   "business to business",
    "b2c":   "business to consumer",
    "p&l":   "profit and loss",
    "pnl":   "profit and loss",
    "eom":   "end of month",
    "eoq":   "end of quarter",
    "eoy":   "end of year",
    "q1":    "first quarter",
    "q2":    "second quarter",
    "q3":    "third quarter",
    "q4":    "fourth quarter",
    "hq":    "headquarters",
    "mgmt":  "management",
    "exec":  "executive",
    "rev":   "revenue",
    "avg":   "average",
    "vol":   "volume",
    "cnt":   "count",
    "pct":   "percent",
    "num":   "number",
    "no":    "number",     # "no of orders" → "number of orders"
    "qty":   "quantity",
    "val":   "value",
    "diff":  "difference",
    "vs":    "versus",
    "w/":    "with",
    "w/o":   "without",
}

# Build a single compiled regex — longest keys first to avoid partial matches
_ABBREV_RE = re.compile(
    r'\b(' + '|'.join(
        re.escape(k) for k in sorted(_ABBREVIATIONS, key=len, reverse=True)
    ) + r')\b',
    re.IGNORECASE,
)

def _expand_abbreviations(text: str) -> str:
    def _replace(m):
        return _ABBREVIATIONS[m.group(0).lower()]
    return _ABBREV_RE.sub(_replace, text)


# ── Domain whitelist — pyspellchecker must never "fix" these ──────────────────
_DOMAIN_WORDS = {
    "avg", "max", "min", "sum", "count", "top", "by", "vs",
    "revenue", "seller", "sellers", "sku", "skus", "order", "orders",
    "delivery", "deliveries", "category", "categories",
    # Abbreviation tokens — never let pyspellchecker mangle these
    "yoy", "mom", "qoq", "wow", "ytd", "mtd", "qtd",
    "aov", "gmv", "ltv", "clv", "cac", "nps", "csat", "otd", "otif",
    "kpi", "kpis", "ebitda", "roas",
    # Brazilian state codes — must never be "corrected"
    "sp", "rj", "mg", "ba", "sc", "rs", "pr", "ce", "go", "pe",
    "es", "am", "pa", "ma", "mt", "ms", "pi", "al", "pb", "rn",
    "se", "to", "ro", "ac", "ap", "rr", "df",
    # Business / analytics proper nouns
    "pareto", "quartile", "quartiles", "percentile", "percentiles",
    "cohort", "cohorts", "histogram", "heatmap", "funnel",
    "decile", "deciles", "outlier", "outliers",
}
_spell.word_frequency.load_words(_DOMAIN_WORDS)

# Numeric/symbolic shorthand
_NUMERIC_SHORTHAND = {
    "b4":     "before",
    "averg":  "average",
    "averag": "average",
    "delivry":"delivery",
    "dlvry":  "delivery",
    "revenu": "revenue",
    "reveneu":"revenue",
}

# Symbol substitutions
_SYMBOL_SUBS = [
    (re.compile(r'\s*&\s*'),  ' and '),
    (re.compile(r'\s*%\s*'),  ' percent '),
    (re.compile(r'\s*>\s*'),  ' greater than '),
    (re.compile(r'\s*<\s*'),  ' less than '),
]


def correct_prompt(text: str) -> str:
    """
    Returns a cleaned version of `text`:
      1. Expands MBA/corporate abbreviations
      2. Substitutes symbols
      3. Applies pyspellchecker typo correction
    """
    if not text or not text.strip():
        return text

    # Step 1 — abbreviation expansion
    text = _expand_abbreviations(text)

    # Step 2 — symbol substitution
    for pattern, replacement in _SYMBOL_SUBS:
        text = pattern.sub(replacement, text)
    text = text.strip()

    # Step 3 — pyspellchecker
    tokens = re.findall(r"[A-Za-z0-9']+|[^A-Za-z0-9']+", text)
    corrected_tokens = []

    for token in tokens:
        if not re.match(r"[A-Za-z0-9']", token):
            corrected_tokens.append(token)
            continue

        lower = token.lower()

        if lower in _NUMERIC_SHORTHAND:
            corrected_tokens.append(_NUMERIC_SHORTHAND[lower])
            continue

        if token.isdigit() or lower in _DOMAIN_WORDS:
            corrected_tokens.append(token)
            continue

        candidate = _spell.correction(lower)
        if candidate and candidate != lower:
            if token[0].isupper():
                candidate = candidate.capitalize()
            corrected_tokens.append(candidate)
        else:
            corrected_tokens.append(token)

    corrected = "".join(corrected_tokens)

    if corrected.lower() != text.lower():
        print(f"{_ts()} [Spellcheck] '{text}' → '{corrected}'")

    return corrected
