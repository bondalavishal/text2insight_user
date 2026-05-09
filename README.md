# text2insight

A Slack-native analytics bot that lets your team ask plain English questions about business data — no SQL knowledge required. text2insight translates natural language into SQL, runs it against Databricks, and returns a clear, human-readable summary right in Slack.

Built on the [Olist Brazilian E-Commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) (2016–2018), covering ~100k orders, ~3k sellers, and 74 product categories.

---

## Features

| Feature | Description |
|---|---|
| **NL → SQL** | Natural language questions translated to SQL via LLM fallback chain |
| **Multi-question** | Ask up to 5 questions at once — answered in parallel with individual progress bars |
| **Semantic cache** | Instant replies for similar questions (ChromaDB, cosine ≥ 0.78) |
| **Self-learning** | Every successful Q+SQL pair saved to a RAG query library for future few-shot examples |
| **Spellcheck** | Offline typo correction + 135 abbreviation expansions (YoY, GMV, TAT, NPS, etc.) |
| **Anomaly detection** | Auto-flags delivery > 20 days, cancellation > 5%, revenue drop > 10%, review < 3.0 |
| **Download** | Reply `download` to get full results as a CSV file |
| **Explain** | Reply `explain` for a structured business analyst deep-dive with key findings, outliers, business implications, and recommended actions |
| **Feedback loop** | Thumbs up/down signals evict bad answers from cache and retrain |
| **Stats** | Reply `text2insight stats` for bot performance metrics |
| **Full interaction logging** | Every interaction (query, download, explain, feedback, greeting) logged to Databricks |
| **Guardrails** | Blocks destructive SQL keywords, restricts to allowed data sources, enforces LIMIT |

---

## Architecture

```
Slack message
    │
    ├─ Spellcheck + abbreviation expansion (offline)
    ├─ Intent classification (regex fast-path → Cerebras slow-path)
    ├─ Download / Explain / Stats / Feedback handlers (bypass SQL pipeline)
    │
    └─ SQL pipeline:
         ├─ Semantic cache lookup (ChromaDB) → instant reply if hit
         ├─ RAG retrieval (few-shot examples from query library)
         ├─ SQL generation (Cerebras → Groq → OpenRouter → Ollama)
         ├─ SQL guardrails (validate + enforce LIMIT)
         ├─ Databricks execution
         ├─ Anomaly detection
         ├─ Plain-English summarisation (same LLM chain)
         ├─ Cache + query library update
         └─ Interaction log → Databricks
```

**LLM fallback chain** (used for SQL generation, summaries, and explanations):
`Cerebras (15s timeout)` → `Groq (7 models, rotate on 429)` → `OpenRouter (26 models, rotate on 429)` → `Ollama (last resort)`

---

## Tech Stack

- **Python 3.11+** — core runtime
- **Slack Bolt** — event handling and Socket Mode
- **Databricks SQL Connector** — query execution against Delta Lake
- **ChromaDB** — semantic cache (`text2insight_cache`) and query library (`text2insight_query_lib`)
- **sentence-transformers / ONNX MiniLM-L6-v2** — local embeddings (no external API needed)
- **Cerebras / Groq / OpenRouter** — LLM providers in fallback chain
- **pyspellchecker** — offline spellcheck
- **Flask** — health check endpoint (`/health`)
- **Docker + docker-compose** — containerised deployment

---

## Project Structure

```
text2insight_user/
├── main.py                          # Slack event loop + full pipeline orchestration
├── start.sh                         # Entrypoint: build RAG index if needed, then start
├── docker-compose.yml               # Docker service definition
├── requirements.txt
├── .env                             # API keys (not committed)
│
├── app/
│   ├── llm/
│   │   ├── sql_generator.py         # SQL generation + LLM fallback chain + all SQL rules
│   │   ├── intent.py                # Intent classifier (greeting/text_to_sql/feedback/oos)
│   │   ├── spellcheck.py            # Abbreviation expansion + typo correction
│   │   └── cerebras_breaker.py      # Circuit breaker for Cerebras timeouts
│   │
│   ├── rag/
│   │   ├── schema_definitions.md    # Full schema reference + critical SQL patterns
│   │   ├── loader.py                # Seeds ChromaDB query library from schema_definitions.md
│   │   ├── retriever.py             # retrieve() + learn_pattern() for self-learning RAG
│   │   └── business_logic.md        # Business rules reference
│   │
│   ├── slack/
│   │   └── handler.py               # summarise_results, detect_anomalies, generate_explanation,
│   │                                #   is_download_request, is_explain_request, get_stats
│   │
│   ├── eval/
│   │   ├── cache.py                 # ChromaDB semantic cache with number-matching guard
│   │   └── interaction_logger.py    # Databricks interaction log writer
│   │
│   └── sql/
│       ├── connector.py             # run_query() — Databricks connection
│       └── guardrails.py            # validate_sql() + enforce_limit()
│
├── run_test.py                      # Wipe logs + run 5 canonical test questions
├── run_test_3x.py                   # Run 3 rounds of the test suite
├── clear_cache.py                   # Wipe ChromaDB cache
├── create_query_log_table.sql       # DDL for default.text2insight_user_query_log
└── migrate_interactions_table.sql   # Phase 8 legacy table migration
```

---

## Database Schema

### Views (use by default)

| View | Key Columns | Use For |
|---|---|---|
| `vw_orders_metrics` | order_id, customer_state, order_revenue, delivery_days | Order-level analysis, TAT, revenue by state |
| `vw_seller_metrics` | seller_id, total_orders, total_revenue, avg_review_score | Seller performance (no time dimension) |
| `vw_product_metrics` | product_id, category, total_orders, avg_price, avg_review_score | Product/category analysis |
| `vw_monthly_revenue` | year, month, total_orders, total_revenue, canceled_orders | Trends, MoM/YoY growth |

> **Important:** `vw_orders_metrics` has `delivery_days INT` (pre-computed) — there are no raw timestamp columns on this view.

### Raw Tables

| Table | Key Columns |
|---|---|
| `olist_orders` | order_id, customer_id, order_status, timestamps |
| `olist_order_items` | order_id, product_id, seller_id, price, freight_value |
| `olist_products` | product_id, product_category_name (Portuguese) |
| `product_category_translation` | product_category_name → product_category_name_english |
| `olist_order_reviews` | order_id, review_score (1–5) |
| `olist_sellers` | seller_id, seller_city, seller_state |
| `olist_customers` | customer_id, customer_unique_id, customer_city, customer_state |
| `olist_order_payments` | order_id, payment_type, payment_installments, payment_value |

---

## Setup

### Prerequisites

- Python 3.11+
- Databricks workspace with a SQL warehouse
- Slack app with **Bot Token** and **App-Level Token** (Socket Mode enabled)
- At least one LLM API key (Groq is recommended as primary fallback)

### 1. Clone and install

```bash
git clone https://github.com/bondalavishal/text2insight_user.git
cd text2insight_user
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

DATABRICKS_SERVER_HOSTNAME=your-workspace.azuredatabricks.net
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/your-warehouse-id
DATABRICKS_TOKEN=dapi...

CEREBRAS_API_KEY=...      # optional but recommended
GROQ_API_KEY=...          # primary fallback
OPENROUTER_API_KEY=...    # secondary fallback
```

### 3. Create the Databricks interaction log table

Run `create_query_log_table.sql` in your Databricks SQL editor:

```sql
-- creates default.text2insight_user_query_log
```

### 4. Build the RAG index

```bash
python3 -m app.rag.loader
```

This seeds the ChromaDB query library with the SQL patterns from `schema_definitions.md`. Only needed once — `start.sh` handles this automatically on first Docker run.

### 5. Start the bot

```bash
# Direct
source venv/bin/activate
python3 main.py

# Docker
docker-compose up --build
```

A health check endpoint runs at `http://localhost:3000/health`.

---

## Usage

Once text2insight is in your Slack workspace, mention it or send a message in any channel it's invited to.

**Single question:**
```
What were the top 10 categories by revenue in 2018?
```

**Multiple questions (up to 5):**
```
1. Which states have the highest average delivery time?
2. Show monthly revenue growth for 2017
3. What's the cancellation rate by payment type?
```

**Follow-up commands:**
```
download              → receive results as a CSV file
explain               → structured business analyst breakdown of the last result
text2insight stats    → bot performance metrics
```

**Feedback:**
```
👍  or  "looks good"   → marks the answer as correct
👎  or  "wrong"        → evicts the answer from cache, prompts a fresh query
```

---

## Testing

```bash
# Wipe all logs and run the 5 canonical analytical questions
python3 run_test.py

# Run 3 full rounds
python3 run_test_3x.py

# Test Databricks connectivity
python3 test_connections.py
```

The 5 canonical test questions cover: seller quartile analysis, state revenue/TAT comparison, MoM growth acceleration, category risk matrix, and Pareto GMV distribution.

---

## Interaction Log

Every interaction is logged to `default.text2insight_user_query_log` in Databricks.

**Interaction types tracked:** `data_query`, `download`, `explain`, `feedback_positive`, `feedback_negative`, `greeting`, `out_of_scope`, `stats`

**Key fields:** `log_id`, `ts`, `user_id`, `email_id`, `status`, `interaction_type`, `question_asked`, `question_answered`, `generated_sql`, `result_json`, `generated_csv`, `latency_ms`, `rows_returned`, `anomaly_count`, `cached`, `similarity_score`, `success_signal`, `self_learned`, `embedding_id`

---

## Known Limitations

- **Cerebras latency** — Cerebras times out consistently on this deployment; every cold request pays a 15s penalty before falling to Groq. Lower `CEREBRAS_TIMEOUT` in `sql_generator.py` or disable Cerebras if not needed.
- **Connection per query** — `connector.py` opens a new Databricks connection for every `run_query()` call. Under high concurrency (5 parallel questions × multiple calls each) this can hit warehouse connection limits.
- **Local/Databricks log drift** — `app/eval/eval_log.csv` and the Databricks table are written independently. Running `run_test.py` wipes the local CSV but not all Databricks rows, causing permanent gaps in log IDs.
- **No time dimension on seller/product views** — `vw_seller_metrics` and `vw_product_metrics` are lifetime aggregates. Trend questions about individual sellers or products require raw table queries.
