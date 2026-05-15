import os
import re
import threading
import httpx
import concurrent.futures
from datetime import datetime
from cerebras.cloud.sdk import Cerebras
from dotenv import load_dotenv
from app.llm.cerebras_breaker import is_open, record_failure, record_success

_ts = lambda: datetime.now().strftime("%H:%M:%S")

load_dotenv()

# ── Cerebras (primary) ────────────────────────────────────────────────────────
_cerebras      = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"

# ── Groq (fallback 1) — full model list, rotate on rate-limit ────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS  = [
    "meta-llama/llama-4-scout-17b-16e-instruct", # 30K TPM
    "llama-3.3-70b-versatile",                   # 12K TPM
    "openai/gpt-oss-120b",                       # 8K TPM
    "openai/gpt-oss-20b",                        # 8K TPM
    "qwen/qwen3-32b",                            # 6K TPM
    "llama-3.1-8b-instant",                      # 6K TPM
    "allam-2-7b",                                # 6K TPM
]

# ── OpenRouter (fallback 2) — full model list, rotate on rate-limit ───────────
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS  = [
    "openrouter/elephant-alpha",                                     # 262K ctx
    "google/gemma-4-26b-a4b-it:free",                               # 262K ctx
    "google/gemma-4-31b-it:free",                                    # 262K ctx
    "nvidia/nemotron-3-super-120b-a12b:free",                       # 262K ctx
    "qwen/qwen3-next-80b-a3b-instruct:free",                        # 262K ctx
    "qwen/qwen3-coder:free",                                         # 262K ctx — code specialist
    "nvidia/nemotron-3-nano-30b-a3b:free",                          # 256K ctx
    "minimax/minimax-m2.5:free",                                     # 196K ctx
    "openai/gpt-oss-120b:free",                                      # 131K ctx
    "openai/gpt-oss-20b:free",                                       # 131K ctx
    "z-ai/glm-4.5-air:free",                                        # 131K ctx
    "arcee-ai/trinity-large-preview:free",                          # 131K ctx
    "nousresearch/hermes-3-llama-3.1-405b:free",                    # 131K ctx
    "nvidia/nemotron-nano-12b-v2-vl:free",                          # 128K ctx
    "nvidia/nemotron-nano-9b-v2:free",                              # 128K ctx
    "meta-llama/llama-3.3-70b-instruct:free",                       # 128K ctx
    "meta-llama/llama-3.2-3b-instruct:free",                        # 128K ctx
    "google/gemma-3-27b-it:free",                                    # 32K ctx
    "google/gemma-3-12b-it:free",                                    # 32K ctx
    "google/gemma-3-4b-it:free",                                     # 32K ctx
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free", # 32K ctx
    "liquid/lfm-2.5-1.2b-thinking:free",                            # 32K ctx
    "liquid/lfm-2.5-1.2b-instruct:free",                            # 32K ctx
    "google/gemma-3n-e4b-it:free",                                   # 8K ctx
    "google/gemma-3n-e2b-it:free",                                   # 8K ctx
]

# ── Ollama (last resort) ──────────────────────────────────────────────────────
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "mannix/defog-llama3-sqlcoder-8b"

from app.rag.retriever import retrieve

# Load full schema and business context once at startup
_RAG_DIR = os.path.join(os.path.dirname(__file__), "..", "rag")
with open(os.path.join(_RAG_DIR, "schema_definitions.md")) as f:
    _FULL_SCHEMA = f.read()
with open(os.path.join(_RAG_DIR, "business_logic.md")) as f:
    _BUSINESS_CONTEXT = f.read()

SQL_PROMPT = """You are an expert Databricks SQL generator. Convert the question into a single SQL query.

STRICT RULES:
- Use views by default. Use raw tables (olist_orders, olist_order_items, olist_products, product_category_translation, olist_order_reviews, olist_sellers, olist_customers, olist_order_payments) ONLY when views cannot answer
- When using raw tables with olist_products: ALWAYS join to product_category_translation on product_category_name to get English category names. English column is t.product_category_name_english (on the translation table alias t) — NEVER p.product_category_name_english
- NEVER join views directly in a FROM/JOIN clause; if a question requires metrics from two different views (e.g. monthly revenue from vw_monthly_revenue AND monthly avg TAT from vw_orders_metrics), use CTEs — one CTE per view — then JOIN the CTEs. This is allowed and correct
- Never invent columns not listed in the schema below
- Never use SUM(*) — use COUNT(*) for row counts
- Never use aggregates in WHERE — use HAVING
- Window functions (LAG, LEAD, PERCENT_RANK, ROW_NUMBER, RANK) CANNOT appear in WHERE or HAVING — store them as columns in a CTE first, then filter on that column in the outer query. Example: never write WHERE LAG(col, 1) > 0; instead assign prev_col = LAG(col,1) in a CTE then WHERE prev_col IS NOT NULL AND prev_col > 0
- Always include LIMIT
- Never use spaces in aliases — underscores only
- For cancellations from vw_orders_metrics: SUM(CASE WHEN order_status = 'canceled' THEN 1 ELSE 0 END)
- For category-level cancellations: join olist_orders + olist_order_items + olist_products + product_category_translation
- For payment analysis: use olist_order_payments (join to olist_orders on order_id)
- For customer geography / state distribution: vw_orders_metrics already has BOTH customer_state AND customer_id as direct columns — ALWAYS use it first for state-level questions. Example: "which state has most customers" → SELECT customer_state, COUNT(DISTINCT customer_id) AS num_customers FROM vw_orders_metrics GROUP BY customer_state ORDER BY num_customers DESC LIMIT 1. Only use olist_customers when you explicitly need customer_unique_id or city-level data not in the view
- For year comparisons: use vw_monthly_revenue GROUP BY year
- olist_orders ONLY has: order_id, customer_id, order_status, timestamps (order_purchase_timestamp, order_approved_at, order_delivered_carrier_date, order_delivered_customer_date, order_estimated_delivery_date) — NO order_date, NO customer_state, NO seller_id, NO order_total, NO price, NO freight_value. NEVER reference order_date on olist_orders — use order_purchase_timestamp (TIMESTAMP) for raw-table date arithmetic, or join vw_orders_metrics to get the pre-cast order_date DATE column
- For customer_state from raw tables: JOIN olist_customers c ON o.customer_id = c.customer_id, then use c.customer_state
- CRITICAL — customer_id in olist_orders is per-order, NOT per-person: the Olist schema assigns a brand-new customer_id for every single order. A person who places 3 orders has 3 different customer_id values in olist_orders. To find customers who placed multiple orders you MUST use customer_unique_id from olist_customers. Correct repeat-customer CTE pattern: WITH customer_orders AS (SELECT c.customer_unique_id, COUNT(DISTINCT o.order_id) AS order_count, MIN(o.order_purchase_timestamp) AS first_order_ts, MAX(o.order_purchase_timestamp) AS last_order_ts FROM olist_orders o JOIN olist_customers c ON o.customer_id = c.customer_id GROUP BY c.customer_unique_id). NEVER GROUP BY o.customer_id for repeat-customer analysis — you will get count=1 for every row
- CRITICAL — olist_order_payments has NO customer_id column: columns are order_id, payment_sequential, payment_type, payment_installments, payment_value. For customer-level payment analysis (lifetime GMV by installment group, etc.) you must bring in customer_id through olist_orders. Correct pattern: WITH payment_orders AS (SELECT o.customer_id, op.payment_type, op.payment_installments, SUM(op.payment_value) AS order_value FROM olist_orders o JOIN olist_order_payments op ON o.order_id = op.order_id GROUP BY o.customer_id, op.payment_type, op.payment_installments). NEVER reference op.customer_id or payment_groups.customer_id — those columns do not exist
- For state + review score combined: use vw_orders_metrics m (has order_id + customer_state) JOIN olist_order_reviews r ON m.order_id = r.order_id — never use olist_orders as the state source
- vw_orders_metrics TAT: the view exposes delivery_days INT (pre-computed, NULL if not delivered) — NEVER reference m.order_delivered_customer_date, m.order_purchase_timestamp, or any timestamp on vw_orders_metrics — those columns do NOT exist on the view. For state-level TAT use AVG(delivery_days) WHERE delivery_days IS NOT NULL
- For seller GMV from raw tables: SUM(i.price + i.freight_value) FROM olist_order_items i GROUP BY i.seller_id — never use olist_orders.order_total (it does not exist)
- For seller + review score combined: olist_order_items i JOIN olist_order_reviews r ON i.order_id = r.order_id GROUP BY i.seller_id
- If the question cannot be answered from available data:
  SELECT 'This question cannot be answered from the available data.' AS message LIMIT 1

AGGREGATION RULES (prevent fan-out bugs):
- COUNT(DISTINCT) for orders: when joining through olist_order_items, ALWAYS use COUNT(DISTINCT o.order_id) for order counts — COUNT(*) counts order-item rows not orders and will be wrong
- For canceled order counts through olist_order_items: use COUNT(DISTINCT CASE WHEN o.order_status = 'canceled' THEN o.order_id END) — not SUM(CASE WHEN ...)
- Payment fan-out: NEVER join olist_order_payments directly to olist_order_items — this multiplies payment rows by item count. Always aggregate payments at order level first in a CTE: WITH order_payments AS (SELECT order_id, SUM(payment_value) AS total_paid FROM olist_order_payments GROUP BY order_id), then join that CTE to olist_orders
- Payment averages: olist_order_payments has one row per installment — never AVG(payment_value) on raw rows; always SUM per order_id first
- Payment type + avg order value pattern — EXACT STRUCTURE: (1) CTE `order_payments` keeps order_id: SELECT order_id, payment_type, SUM(payment_value) AS total_paid FROM olist_order_payments GROUP BY order_id, payment_type — NEVER group by payment_type alone here (loses order_id needed for the join); (2) CTE `order_items` aggregates GMV: SELECT order_id, SUM(price + freight_value) AS order_gmv FROM olist_order_items GROUP BY order_id; (3) final SELECT: JOIN order_payments op to order_items oi ON op.order_id = oi.order_id, then GROUP BY op.payment_type, computing COUNT(DISTINCT op.order_id) AS num_orders and AVG(oi.order_gmv) AS avg_order_value
- TAT delivery filter: when computing delivery time from raw tables, always filter WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL to exclude undelivered orders from the average
- MoM growth rate formula: ALWAYS parenthesize correctly — ROUND((current - prev) * 100.0 / NULLIF(prev, 0), 2). NEVER inline LAG() in arithmetic: `value - LAG() * 1.0 / LAG()` evaluates as `value - 1.0` due to operator precedence — always store LAG() result as a named column in a prior CTE, then reference that column
- YoY quarterly growth pattern — EXACT STRUCTURE: (1) CTE `quarterly` groups by order_year + CEIL(order_month/3.0) AS quarter, computes SUM(order_revenue) AS total_revenue + COUNT(DISTINCT order_id) AS total_orders; (2) CTE `lagged` selects ALL columns from quarterly PLUS LAG(total_revenue, 4) OVER (ORDER BY order_year, quarter) AS prev_revenue AND LAG(total_orders, 4) OVER (ORDER BY order_year, quarter) AS prev_orders — offset MUST be 4 (same quarter prior year), NEVER 1 (that is QoQ); (3) final SELECT reads order_year, quarter, total_revenue, total_orders, ROUND((total_revenue-prev_revenue)*100.0/NULLIF(prev_revenue,0),2) AS rev_growth_pct, ROUND((total_orders-prev_orders)*100.0/NULLIF(prev_orders,0),2) AS order_growth_pct FROM lagged WHERE prev_revenue IS NOT NULL — NEVER add a third CTE that drops total_revenue/total_orders then tries to read them in the outer SELECT (column-not-found crash)
- Multi-CTE column references: in the outer SELECT verify every column alias against the CTE that owns it. Example: if avg_tat comes from CTE `t`, write t.avg_tat — never s.avg_tat (where s is a different CTE). Wrong alias = Databricks column-not-found crash
- Risk/penalty matrix WHERE filter: NEVER add WHERE order_status = 'delivered' when computing cancel_rate — canceled orders would be excluded, making cancel_rate = 0 everywhere. Instead use all orders and compute: cancel_rate = COUNT(DISTINCT CASE WHEN o.order_status = 'canceled' THEN o.order_id END) * 100.0 / NULLIF(COUNT(DISTINCT o.order_id), 0). For avg TAT in the same query use AVG(CASE WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL THEN DATEDIFF(o.order_delivered_customer_date, o.order_purchase_timestamp) END) so both metrics come from one pass over all orders

RANKING AND SCORING RULES (prevent inverted rankings):
- Window functions on aggregates: ALWAYS use a CTE when applying PERCENT_RANK(), ROW_NUMBER(), LAG(), or any window function over aggregated values — compute GROUP BY aggregates in the CTE first, then apply window functions in the outer SELECT. Never inline window functions in the same SELECT as GROUP BY aggregates
- PERCENT_RANK direction for performance scores: for metrics where higher = better (revenue, review_score, order_count), use ORDER BY metric ASC → 1.0 = best. For metrics where lower = better (TAT, cancel_rate), use ORDER BY metric DESC → 1.0 = best. This is for ranking performance (best gets 1.0)
- PERCENT_RANK direction for RISK scores — EXACT ORDERING (no exceptions):
  cancel_risk: PERCENT_RANK() OVER (ORDER BY cancel_rate ASC) — 1.0 = highest cancel = most risky
  review_risk: PERCENT_RANK() OVER (ORDER BY avg_review_score DESC) — 1.0 = lowest review = most risky — USE DIRECTLY in formula, NEVER as (1 - review_risk)
  tat_risk: PERCENT_RANK() OVER (ORDER BY avg_tat_days ASC) — 1.0 = longest TAT = most risky — ALWAYS ASC, NEVER DESC
  Risk formula: ROUND((cancel_risk * 0.4 + review_risk * 0.4 + tat_risk * 0.2), 3) — weights must sum to 1.0, no inversion transforms
- Quartile thresholds — EXACT RULE: "top quartile of metric X" (highest 25%) with ORDER BY X ASC → filter rank >= 0.75. "Bottom quartile of metric X" (lowest 25%) with ORDER BY X ASC → filter rank <= 0.25. With ORDER BY X DESC the thresholds flip. EXAMPLE: top quartile order volume (ORDER BY order_count ASC) → order_rank >= 0.75 NOT <= 0.25
- NULL exclusion before ranking: when PERCENT_RANK()ing a metric that can be NULL (e.g., avg TAT for sellers with no deliveries), filter out NULL rows in the CTE before applying the window function — NULLs corrupt rank distribution and can score as best performers
- Composite score normalization: before applying weights (e.g., 40% + 30% + 30%), ALL components must be on the same 0–1 scale using PERCENT_RANK() or (value - MIN(value)) / NULLIF(MAX(value) - MIN(value), 0). Never weight raw values on different scales — the largest-scale component will dominate regardless of its intended weight
- Minimum sample for rankings: for any risk matrix, health index, or performance ranking over categories or sellers, add HAVING COUNT(DISTINCT o.order_id) >= 10 to exclude statistically unreliable micro-segments — unless the user explicitly asks to include all
- Pareto / cumulative share (e.g. "top sellers making up 80% of GMV"): compute cumulative metric with SUM(metric) OVER (ORDER BY metric DESC ROWS UNBOUNDED PRECEDING) AS cum_metric in a CTE, join to a total CTE, then group by CASE WHEN cum_metric - metric < 0.8 * total THEN 'pareto' ELSE 'rest' END. NEVER use PERCENTILE_CONT for this — it gives the 80th-percentile value, not the cumulative 80% cutoff
- Lorenz curve / revenue decile pattern (e.g. "at each decile what % of total revenue captured"): EXACT 4-CTE STRUCTURE: (1) CTE `seller_revenue`: SELECT seller_id, SUM(price + freight_value) AS total_revenue FROM olist_order_items GROUP BY seller_id; (2) CTE `ranked`: SELECT seller_id, total_revenue, NTILE(10) OVER (ORDER BY total_revenue ASC) AS revenue_decile FROM seller_revenue; (3) CTE `decile_totals`: SELECT revenue_decile, SUM(total_revenue) AS decile_revenue FROM ranked GROUP BY revenue_decile; (4) CTE `cumulative`: SELECT revenue_decile, SUM(decile_revenue) OVER (ORDER BY revenue_decile ROWS UNBOUNDED PRECEDING) AS cumulative_revenue, SUM(decile_revenue) OVER () AS grand_total FROM decile_totals; final SELECT: revenue_decile * 10 AS seller_percentile, ROUND(cumulative_revenue * 100.0 / grand_total, 1) AS cumulative_revenue_pct ORDER BY revenue_decile LIMIT 10
- Repeat vs one-time customer comparison (multi-metric): EXACT 4-CTE STRUCTURE: (1) CTE `customer_order_count`: SELECT c.customer_unique_id, COUNT(DISTINCT o.order_id) AS order_count FROM olist_orders o JOIN olist_customers c ON o.customer_id = c.customer_id GROUP BY c.customer_unique_id; (2) CTE `customer_type`: SELECT customer_unique_id, CASE WHEN order_count >= 2 THEN 'Repeat Customer' ELSE 'One-Time Buyer' END AS segment FROM customer_order_count; (3) CTE `order_level`: SELECT o.order_id, o.customer_id, SUM(i.price) AS order_value, COUNT(i.product_id) AS item_count, SUM(i.freight_value) AS freight_paid FROM olist_orders o JOIN olist_order_items i ON o.order_id = i.order_id GROUP BY o.order_id, o.customer_id; (4) final SELECT: ct.segment, ROUND(AVG(ol.order_value),2) AS avg_order_value, ROUND(AVG(ol.item_count),2) AS avg_items_per_order, ROUND(AVG(ol.freight_paid),2) AS avg_freight_paid FROM order_level ol JOIN olist_customers c ON ol.customer_id = c.customer_id JOIN customer_type ct ON c.customer_unique_id = ct.customer_unique_id GROUP BY ct.segment ORDER BY ct.segment LIMIT 10
- Pareto + review scores: to add avg_review_score per group, compute seller-level avg in a separate seller_reviews CTE first, LEFT JOIN that CTE to labeled on seller_id, then in the final SELECT use GROUP BY grp ONLY (not by avg_review_score) and compute ROUND(AVG(sr.avg_review_score), 3) — NEVER include sr.avg_review_score in GROUP BY, it turns each seller into its own group
- Category names in multi-CTE queries: if the final SELECT needs t.product_category_name_english, the final SELECT MUST include explicit JOINs: `FROM olist_order_items i JOIN olist_products p ON i.product_id = p.product_id JOIN product_category_translation t ON p.product_category_name = t.product_category_name JOIN ranked_sellers rs ON i.seller_id = rs.seller_id` — aliases from inner CTEs do not propagate out
- Seller quartile → categories (NEVER use grp/CASE WHEN here): for "top quartile order volume, bottom quartile review → categories", use PERCENT_RANK in a CTE, filter with WHERE in a second CTE, then join to olist_order_items + olist_products + translation. NEVER create a grp column or CASE WHEN group label in this pattern — that is only for Pareto queries. "bottom quartile review score" (lowest reviews) = ORDER BY avg_review_score ASC + review_rank <= 0.25
- State revenue/TAT vs platform average: compute avg_revenue and avg_tat from vw_orders_metrics ALONE (no review join). Review scores require a separate CTE joining vw_orders_metrics m to olist_order_reviews r. Never compute revenue or delivery_days averages from a review-joined subquery — the inner join to reviews excludes unreviewed orders and shifts both averages so that no state passes the filter
- "State revenue" vs "average order value": when a question mentions "states with above-average revenue", use SUM(order_revenue) per state compared to AVG(state totals). Using AVG(order_revenue) is "average order size" — states with high total revenue (SP, RJ, MG) will have LOW per-order avg because they have many small orders, causing 0 results. Use SUM for market size; use AVG only when the question explicitly says "average order value" or "average basket size"

---

FULL DATABASE SCHEMA:
{schema}

---

BUSINESS CONTEXT:
{business_context}

---

SIMILAR PAST QUESTIONS AND SQL (use as reference if relevant — not required):
{few_shot_examples}

---

Question: {question}

Reply with ONLY the SQL query inside a ```sql fence. No explanation, no commentary."""


def _extract_sql(raw: str) -> str:
    fence_match = re.search(r'```sql\s*(.*?)(?:```|$)', raw, re.DOTALL | re.IGNORECASE)
    if fence_match:
        sql = fence_match.group(1).strip()
    else:
        plain_match = re.search(r'```\s*(.*?)(?:```|$)', raw, re.DOTALL)
        if plain_match:
            sql = plain_match.group(1).strip()
        else:
            fallback = re.search(r'(?im)^(WITH|SELECT)\b', raw)
            if not fallback:
                raise ValueError("No SQL found in LLM response — skipping this provider")
            sql = raw[fallback.start():].strip()
    sql = sql.rstrip(";").strip()
    # Reject reasoning/chain-of-thought responses that slipped past fence detection
    if not re.match(r'(?i)^\s*(WITH|SELECT)\b', sql):
        raise ValueError("LLM returned reasoning text instead of SQL — skipping this provider")
    sql = re.sub(r'(?i)^(SELECT\s+)+', 'SELECT ', sql)
    sql = sql.replace("p.product_category_name_english", "t.product_category_name_english")
    return sql.strip()


_CEREBRAS_SQL_TIMEOUT = 5    # hard wall-clock seconds before falling to Groq
_cerebras_lock = threading.Lock()  # serialize parallel calls so 2nd+ see open circuit

def _via_cerebras(prompt: str) -> str:
    with _cerebras_lock:
        if is_open():
            raise RuntimeError("Cerebras circuit open")

        def _call():
            return _cerebras.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=512,
            ).choices[0].message.content.strip()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future   = executor.submit(_call)
        try:
            result = future.result(timeout=_CEREBRAS_SQL_TIMEOUT)
            executor.shutdown(wait=False)
            record_success()
            return result
        except concurrent.futures.TimeoutError:
            executor.shutdown(wait=False)  # release lock without waiting for Cerebras HTTP call
            record_failure()
            raise RuntimeError(f"Cerebras SQL timed out after {_CEREBRAS_SQL_TIMEOUT}s")
        except Exception as e:
            executor.shutdown(wait=False)
            record_failure()
            raise


def _via_groq(prompt: str) -> str:
    """Try each Groq model in order — skip to next on rate-limit (429)."""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")
    last_err = None
    for model in GROQ_MODELS:
        try:
            response = httpx.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       model,
                    "messages":    [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens":  512,
                },
                timeout=45,
            )
            if response.status_code in (429, 413):
                print(f"{_ts()} [LLM] Groq {model} skipped ({response.status_code}) — trying next model")
                last_err = RuntimeError(f"Groq {model} skipped ({response.status_code})")
                continue
            response.raise_for_status()
            print(f"{_ts()} [LLM] SQL via Groq ({model})")
            return response.json()["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            last_err = e
            print(f"{_ts()} [LLM] Groq {model} error ({e}) — trying next model")
            continue
    raise last_err or RuntimeError("All Groq models failed")


def _via_openrouter(prompt: str) -> str:
    """Rotate through all OpenRouter models — skip to next on rate-limit (429)."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    last_err = None
    for model in OPENROUTER_MODELS:
        try:
            response = httpx.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://text2insight.app",
                    "X-Title":       "text2insight",
                },
                json={
                    "model":       model,
                    "messages":    [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens":  512,
                },
                timeout=60,
            )
            if response.status_code == 429:
                print(f"{_ts()} [LLM] OpenRouter {model} rate-limited — trying next model")
                last_err = RuntimeError(f"OpenRouter {model} rate-limited")
                continue
            response.raise_for_status()
            print(f"{_ts()} [LLM] SQL via OpenRouter ({model})")
            return response.json()["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            last_err = e
            print(f"{_ts()} [LLM] OpenRouter {model} error ({e}) — trying next model")
            continue
    raise last_err or RuntimeError("All OpenRouter models failed")


def _via_ollama(prompt: str) -> str:
    response = httpx.post(
        OLLAMA_URL,
        json={
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
            "stream":  False,
            "options": {"temperature": 0, "num_predict": 512, "stop": ["###", "\n\n\n"]}
        },
        timeout=120,
    )
    return response.json()["response"].strip()


def generate_sql(question: str) -> str:
    few_shot = retrieve(question)
    prompt = SQL_PROMPT.format(
        question=question,
        schema=_FULL_SCHEMA,
        business_context=_BUSINESS_CONTEXT,
        few_shot_examples=few_shot if few_shot else "No similar examples yet — generate from schema.",
    )

    # 1. Cerebras
    try:
        raw = _via_cerebras(prompt)
        print(f"{_ts()} [LLM] SQL via Cerebras")
        return _extract_sql(raw)
    except Exception as e:
        print(f"{_ts()} [LLM] Cerebras failed ({e}) — trying Groq")

    # 2. Groq
    try:
        raw = _via_groq(prompt)
        return _extract_sql(raw)
    except Exception as e:
        print(f"{_ts()} [LLM] Groq failed ({e}) — trying OpenRouter")

    # 3. OpenRouter
    try:
        raw = _via_openrouter(prompt)
        return _extract_sql(raw)
    except Exception as e:
        print(f"{_ts()} [LLM] OpenRouter failed ({e}) — trying Ollama")

    # 4. Ollama (last resort)
    try:
        raw = _via_ollama(prompt)
        print(f"{_ts()} [LLM] SQL via Ollama (last resort)")
        return _extract_sql(raw)
    except Exception as e:
        print(f"{_ts()} [LLM] Ollama also failed ({e})")
        raise RuntimeError("All LLM providers unavailable for SQL generation.")
