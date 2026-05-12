# text2insight — Full System Overview for Visual Representation

---

## What Is text2insight?

**text2insight** is a Slack-native analytics assistant that allows anyone on a team to ask plain-English business questions and receive data-backed answers — without writing a single line of SQL.

- **Input:** A natural language question typed in Slack
- **Output:** A plain-English summary, anomaly flags, optional CSV download, and a structured business analyst explanation
- **Data source:** Databricks Delta Lake (Olist Brazilian E-Commerce dataset, 2016–2018)
- **No SQL knowledge required** — the bot handles everything from translation to execution to summarisation

---

## The Problem It Solves

| Old Way | With text2insight |
|---|---|
| Ask a data analyst to write a query | Ask the bot directly in Slack |
| Wait hours or days for results | Get an answer in under 15 seconds |
| Requires SQL expertise | Plain English only |
| No context around numbers | Human-readable summary + anomaly flags |
| Results live in spreadsheets | Results arrive in Slack + CSV on demand |
| No audit trail | Every interaction logged to Databricks |

---

## Dataset at a Glance

| Metric | Value |
|---|---|
| Total Orders | ~100,000 |
| Active Sellers | ~3,000 |
| Product Categories | 74 |
| Customer States | 27 (all of Brazil) |
| Time Period | 2016 – 2018 |
| Payment Methods | Credit card, boleto, voucher, debit card |
| Data Platform | Databricks Delta Lake (cloud) |

---

## End-to-End Message Flow

### Stage 1 — Message Arrives in Slack
- User sends a message in a Slack channel (must @mention the bot) or a direct message
- Bot receives the message via Socket Mode (no public URL required)

### Stage 2 — Pre-Processing
- **Spellcheck:** 135 domain abbreviations expanded (YoY → Year over Year, GMV → Gross Merchandise Value, TAT → Turnaround Time, NPS → Net Promoter Score, etc.) + offline typo correction
- **Intent Classification:** Fast regex check first; if inconclusive, LLM classifies into one of 4 intents:
  - `text_to_sql` — data question
  - `greeting` — conversational opener
  - `feedback_positive` or `feedback_negative` — thumbs up/down signal
  - `out_of_scope` — non-data question

### Stage 3 — Special Command Routing (bypass SQL pipeline)
- `download` → serves all results from last session as CSV files
- `explain` → generates structured business analyst breakdown for all answered questions
- `stats` → shows bot performance metrics (pass rate, cache hit rate, avg latency)
- `feedback` → updates success signal in Databricks, evicts bad answers from cache

### Stage 4 — SQL Pipeline (for data questions)
1. **Semantic Cache Lookup** — checks ChromaDB for a semantically similar question answered before (cosine similarity ≥ 0.78). If hit → instant reply, skip steps 2–7
2. **RAG Retrieval** — fetches up to 5 similar past question+SQL pairs from the query library as few-shot examples for the LLM
3. **SQL Generation** — LLM generates Databricks SQL using the full schema, business rules, and few-shot examples
4. **SQL Guardrails** — blocks destructive keywords (DROP, DELETE, INSERT, UPDATE), restricts to allowed tables/views, enforces LIMIT clause
5. **Databricks Execution** — runs the validated SQL against the Delta Lake warehouse
6. **Anomaly Detection** — auto-flags: delivery > 20 days, cancellation rate > 5%, revenue drop > 10% MoM, review score < 3.0
7. **Plain-English Summary** — LLM converts raw rows into a human-readable paragraph with business context
8. **Cache + Query Library Update** — saves the question+SQL+summary for future cache hits and few-shot examples
9. **Interaction Logging** — full record written to `default.text2insight_user_query_log` in Databricks

---

## LLM Fallback Chain

The same chain is used for SQL generation, summarisation, and explanations.

```
Cerebras  (primary)     →  5s timeout, serialized to prevent parallel overload
    ↓ on failure / 429
Groq      (fallback 1)  →  7 models, rotates on 429 / rate limit
    ↓ all Groq models exhausted
OpenRouter (fallback 2) →  25 models, rotates on 429 / rate limit
    ↓ all OpenRouter models exhausted
Ollama    (last resort)  →  local model, always available
```

### Why This Chain?
- **Cerebras:** Fastest inference (sub-second when available), best SQL quality
- **Groq:** Free tier, multiple models, very fast (~2–3s per call)
- **OpenRouter:** Wide model diversity, handles edge cases
- **Ollama:** Offline fallback — bot never fully goes down

### Circuit Breaker
- Tracks Cerebras failures in a 5-minute rolling window
- After 2 failures → circuit opens, skips Cerebras for 3 minutes
- Serialization lock on Cerebras: only one thread calls it at a time — when it fails, all queued threads immediately skip to Groq

---

## Multi-Question Parallel Processing

Users can ask up to **5 questions at once**, numbered or line-separated.

```
@text2insight
1. Which states have the highest revenue?
2. What is the cancellation rate by payment type?
3. Show monthly revenue growth for 2017
4. Who are the top 10 sellers by order volume?
5. Which categories have the worst review scores?
```

**What happens:**
- Bot posts 5 individual progress bar messages simultaneously
- All 5 questions execute in parallel using a thread pool (max 5 workers)
- Each message updates live as its question completes (0% → 20% → 40% → 60% → 80% → 100%)
- After all 5 complete: user can type `@text2insight explain` for breakdowns on all 5, or `@text2insight download` for all 5 CSVs

---

## Semantic Cache — How It Works

| Step | Detail |
|---|---|
| Embedding model | MiniLM-L6-v2 (ONNX, runs locally — no API call) |
| Vector store | ChromaDB (persistent, local disk) |
| Similarity threshold | Cosine ≥ 0.78 — below this, treat as a new question |
| Number guard | Questions with different numbers (top 5 vs top 10) are never served the same cached answer |
| Cache hit latency | < 500ms (vs 5–30s for a fresh query) |
| Cache miss | Full SQL pipeline runs; result is stored for future hits |
| Bad answer eviction | Thumbs-down feedback removes the entry from cache and marks it in Databricks |

---

## Self-Learning RAG (Retrieval-Augmented Generation)

Every successful question-SQL pair is saved to a query library (`text2insight_query_lib` in ChromaDB).

**How it improves over time:**
1. First time a question type is asked → LLM generates SQL from schema alone
2. Similar questions later → LLM gets 1–5 matching past examples as few-shot context
3. SQL quality improves as the library grows — less hallucination, better pattern adherence

**Query library contents:** question text, validated SQL, log_id reference, timestamp

---

## Anomaly Detection — Auto-Flags

These fire automatically after every query result, with no configuration needed.

| Anomaly Type | Trigger Condition | Example Alert |
|---|---|---|
| Late delivery | Average delivery days > 20 | ⚠️ RJ avg delivery is 28 days — exceeds 20-day threshold |
| High cancellation | Cancel rate > 5% | ⚠️ Cancellation rate in March 2018 is 7.2% — exceeds 5% |
| Revenue drop | MoM revenue change < -10% | ⚠️ Revenue dropped 14.3% in November 2017 |
| Low review score | Avg review score < 3.0 | ⚠️ Seller review is 2.5 — below 3.0 |

---

## Explain Feature — Business Analyst Deep-Dive

When user replies `explain` (or `@text2insight explain` in a channel):

The bot generates a structured breakdown for **every** question answered in the last session:

### Structure of Each Explanation
1. **Overall Picture** — the dominant pattern or trend in the data
2. **Key Findings** — 3–5 specific data points with exact numbers
3. **Outliers & Anomalies** — values that deviate significantly from the norm
4. **Business Implications** — what this means for the business (revenue risk, customer impact, etc.)
5. **Recommended Actions** — 3–5 prioritised, first-person recommendations naming specific metrics and targets

---

## Interaction Logging — Full Audit Trail

Every bot interaction is logged to Databricks table `default.text2insight_user_query_log`.

### Interaction Types Logged
| Type | When |
|---|---|
| `data_query` | Any SQL question answered |
| `download` | User requested CSV |
| `explain` | User requested deep-dive |
| `feedback_positive` | Thumbs up or "looks good" |
| `feedback_negative` | Thumbs down or "wrong" |
| `greeting` | Hello, hi, etc. |
| `out_of_scope` | Non-data questions |
| `stats` | Performance dashboard request |

### Key Fields Captured
- `log_id`, `ts`, `user_id`, `email_id`, `full_name`
- `interaction_type`, `status` (success / failed / cache_hit / blocked)
- `question_asked`, `question_answered`
- `generated_sql`, `result_json`, `generated_csv`
- `latency_ms`, `rows_returned`, `anomaly_count`
- `cached` (bool), `similarity_score`, `self_learned` (bool)
- `success_signal` (positive / negative / null), `embedding_id`

---

## SQL Guardrails — Safety Layer

Before any SQL touches Databricks, it passes through validation:

| Check | Action on Failure |
|---|---|
| Blocked keywords: DROP, DELETE, INSERT, UPDATE, ALTER, TRUNCATE, EXEC | Blocked — user told to rephrase |
| Disallowed data sources (non-Olist tables) | Blocked with reason |
| Must start with SELECT or WITH | Blocked — invalid SQL |
| No LIMIT clause | Enforced automatically (adds LIMIT 100) |
| Window functions in WHERE/HAVING | Caught by SQL rules, regenerated |

---

## Tech Stack Summary

| Layer | Technology | Role |
|---|---|---|
| Interface | Slack Bolt + Socket Mode | Receives and responds to messages |
| Orchestration | Python 3.11 + ThreadPoolExecutor | Parallel question handling |
| LLM (primary) | Cerebras (Qwen-3 235B) | SQL generation + summarisation |
| LLM (fallback 1) | Groq (7 models) | Fast fallback, free tier |
| LLM (fallback 2) | OpenRouter (25 models) | Wide model diversity |
| LLM (last resort) | Ollama (local) | Always-on offline fallback |
| Data warehouse | Databricks Delta Lake | SQL execution |
| Vector store | ChromaDB (local) | Semantic cache + query library |
| Embeddings | MiniLM-L6-v2 ONNX | Local, no API cost |
| Spellcheck | pyspellchecker (offline) | Typo correction |
| Logging | Databricks Delta table | Full interaction audit trail |
| Health check | Flask `/health` endpoint | Uptime monitoring |
| Deployment | Docker + docker-compose | Containerised, single command |

---

## Database Schema — 4 Views + 8 Raw Tables

### Views (used by default for all queries)

| View | Covers | Key Columns |
|---|---|---|
| `vw_orders_metrics` | Order-level detail | order_id, customer_state, order_revenue, delivery_days |
| `vw_seller_metrics` | Seller performance (lifetime) | seller_id, total_orders, total_revenue, avg_review_score |
| `vw_product_metrics` | Category/product analysis | product_id, category, total_orders, avg_price, avg_review_score |
| `vw_monthly_revenue` | Revenue trends over time | year, month, total_orders, total_revenue, canceled_orders |

### Raw Tables (used for joins views can't do)

| Table | Contains |
|---|---|
| `olist_orders` | Order status + timestamps |
| `olist_order_items` | Product + seller + price per line item |
| `olist_products` | Product details (Portuguese category name) |
| `product_category_translation` | Portuguese → English category names |
| `olist_order_reviews` | Review score (1–5) per order |
| `olist_sellers` | Seller city + state |
| `olist_customers` | Customer city + state + unique ID |
| `olist_order_payments` | Payment type + installments + value |

---

## Performance Characteristics

| Metric | Value |
|---|---|
| Cache hit response time | < 500ms |
| Typical fresh query (Groq) | 5–15 seconds |
| Parallel batch of 5 questions | 10–20 seconds total |
| Max questions per message | 5 |
| Cerebras per-call timeout | 5 seconds |
| Groq fallback models available | 7 |
| OpenRouter fallback models available | 25 |
| Similarity threshold for cache hit | Cosine ≥ 0.78 |
| Anomaly detection rules | 4 auto-firing thresholds |
| Abbreviation expansions | 135 |
| Interaction types logged | 8 |

---

## User Commands Reference

| Command | Where | What Happens |
|---|---|---|
| Ask any data question | DM or @mention in channel | Full SQL pipeline → answer in Slack |
| Ask up to 5 questions numbered 1–5 | DM or @mention | Parallel execution, one progress bar each |
| `download` | DM or `@text2insight download` | CSV files uploaded for all answered questions |
| `explain` | DM or `@text2insight explain` | Business analyst breakdown for all answered questions |
| `text2insight stats` | Anywhere | Bot performance dashboard |
| 👍 / "looks good" | After a reply | Marks answer as correct in Databricks |
| 👎 / "wrong" | After a reply | Evicts answer from cache, flags in Databricks |

---

## Feedback Loop — Continuous Improvement

```
User asks question
       ↓
Bot answers + logs to Databricks (success_signal = null)
       ↓
User reacts with 👍 or 👎
       ↓
    👍 Positive                    👎 Negative
       ↓                                ↓
success_signal = 'positive'    Evict from ChromaDB cache
logged to Databricks           success_signal = 'negative'
                               self_learned = FALSE
                               User prompted to ask again
                               Fresh SQL generated on retry
```

---

## Deployment

```
git clone https://github.com/bondalavishal/text2insight_user.git
cd text2insight_user
cp .env.example .env        # fill in your API keys
docker-compose up --build   # single command to launch
```

**Health check:** `http://localhost:3000/health` — returns `ok` when the bot is running.

**Required credentials:**
- Slack Bot Token + App-Level Token (Socket Mode)
- Databricks server hostname + HTTP path + access token
- At least one of: Groq API key (recommended), Cerebras API key, OpenRouter API key

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Slack Socket Mode (no webhook) | No public URL needed — works on local machine or private server |
| Local embeddings (MiniLM ONNX) | Zero API cost for semantic search, runs offline |
| LLM fallback chain (4 providers) | Bot never goes fully down; degrades gracefully |
| Cerebras serialization lock | Prevents 5 parallel calls stacking up and waiting 60s+ each |
| ChromaDB for cache + query lib | Single dependency handles both semantic cache and RAG store |
| Databricks for all logging | Single source of truth — queryable alongside the business data |
| Views as primary query target | Consistent column names, pre-computed metrics, prevents model from inventing columns |
| `explain_items` list per session | After batch questions, explain covers ALL answered questions not just the last |
| DM vs channel routing separation | Prevents double-processing bug where both message + mention handlers fire |

---

## Summary — One-Line Descriptions of Each Component

- **main.py** — Slack event loop, routes messages, orchestrates the full pipeline, manages `_last_interaction` state per user
- **sql_generator.py** — Builds LLM prompt with schema + rules + few-shot examples, calls the 4-provider fallback chain, extracts + cleans SQL
- **intent.py** — Classifies incoming text into greeting / text_to_sql / feedback / out_of_scope
- **spellcheck.py** — Expands 135 business abbreviations, corrects typos offline
- **cerebras_breaker.py** — Circuit breaker: tracks failures in 5-min window, opens after 2, retries after 3 min
- **cache.py** — ChromaDB semantic cache: embeds questions, finds similar hits above threshold, stores + retrieves
- **retriever.py** — ChromaDB query library: stores successful Q+SQL pairs, retrieves few-shot examples for new questions
- **connector.py** — Opens Databricks connection, runs SQL, returns list of row dicts
- **guardrails.py** — Validates SQL for blocked keywords, source restrictions, enforces LIMIT
- **handler.py** — Summarises results in plain English, detects anomalies, generates structured explanations
- **interaction_logger.py** — Writes every interaction to Databricks with full metadata
