# Olist E-Commerce Schema Definitions

## Database
Platform: Databricks Free Edition.
ROUTING RULE: Use VIEWS by default. Use RAW TABLES only when views cannot answer the question.
ONE source per query. NEVER join views. NEVER invent columns.

## WHEN TO USE VIEWS (default)
- Revenue, orders, delivery, seller rankings, product/category performance → use views
- Any question answerable from a single view → always prefer the view

## WHEN TO USE RAW TABLES
- Category-level cancellations → views have no category+status combination
- Freight cost as % of price per category → needs olist_order_items + olist_products
- Review scores per product or category from raw data → olist_order_reviews + olist_products
- Seller performance by category → olist_order_items + olist_products + olist_sellers
- Payment method analysis → olist_order_payments
- Questions explicitly needing joins across order + product + seller dimensions

---

## VIEWS (use by default)

## vw_orders_metrics
Use for: order revenue, delivery performance, order status, customer location.
NO category column. NO seller columns. NO year_month column.
NO raw timestamps — use delivery_days INT for TAT. order_delivered_customer_date does NOT exist on this view.
```sql
CREATE VIEW vw_orders_metrics AS SELECT
    order_id       STRING,
    customer_id    STRING,
    order_status   STRING,   -- delivered/shipped/canceled/unavailable/invoiced/processing/created/approved
    order_date     DATE,
    order_year     INT,
    order_month    INT,      -- 1-12
    customer_city  STRING,
    customer_state STRING,   -- 2-letter Brazilian state code e.g. SP, RJ, MG
    order_revenue  DECIMAL,  -- item prices only, excludes freight
    order_freight  DECIMAL,  -- shipping cost
    order_total    DECIMAL,  -- order_revenue + order_freight
    item_count     INT,
    delivery_days  INT       -- NULL if not delivered; use AVG(delivery_days) for TAT — no timestamp columns exist
FROM ...;
```

## vw_seller_metrics
Use for: seller rankings, seller revenue, seller review scores.
NO time dimension. NO year. NO month. NO date.
```sql
CREATE VIEW vw_seller_metrics AS SELECT
    seller_id        STRING,
    seller_city      STRING,
    seller_state     STRING,
    total_orders     INT,
    total_revenue    DECIMAL,
    avg_order_value  DECIMAL,
    unique_products  INT,
    avg_review_score DECIMAL,
    total_reviews    INT
FROM ...;
```

## vw_product_metrics
Use for: product rankings, category analysis, pricing analysis.
NO time dimension. NO order_year. NO canceled_orders.
```sql
CREATE VIEW vw_product_metrics AS SELECT
    product_id       STRING,
    category         STRING,   -- English name e.g. health_beauty, computers
    product_weight_g INT,
    total_orders     INT,
    total_revenue    DECIMAL,
    avg_price        DECIMAL,
    avg_review_score DECIMAL
FROM ...;
```

## vw_monthly_revenue
Use for: revenue trends, month over month analysis, growth, anomaly detection.
NO order-level columns. NO delivery_days. NO customer columns.
```sql
CREATE VIEW vw_monthly_revenue AS SELECT
    year             INT,
    month            INT,
    year_month       STRING,  -- e.g. '2017-01'
    total_orders     INT,
    total_revenue    DECIMAL,
    avg_order_value  DECIMAL,
    unique_customers INT,
    canceled_orders  INT
FROM ...;
```

---

## RAW TABLES (use only when views cannot answer)

## olist_orders
Use for: order status + date analysis when joining to other raw tables.
**COLUMNS THAT DO NOT EXIST on olist_orders:** `customer_state`, `seller_id`, `order_total`, `price`, `freight_value`.
- For customer_state: JOIN olist_customers c ON o.customer_id = c.customer_id → use c.customer_state
- For seller_id: use olist_order_items
- For order_total / GMV: use vw_orders_metrics (view) or SUM(i.price + i.freight_value) from olist_order_items
```sql
SELECT
    order_id       STRING,
    customer_id    STRING,
    order_status   STRING,   -- delivered/shipped/canceled/unavailable/etc
    order_purchase_timestamp  TIMESTAMP,
    order_approved_at         TIMESTAMP,
    order_delivered_carrier_date   TIMESTAMP,
    order_delivered_customer_date  TIMESTAMP,
    order_estimated_delivery_date  TIMESTAMP
FROM olist_orders;
```

## olist_order_items
Use for: joining orders to products/sellers, freight vs price analysis per category.
Key join table — links order_id → product_id → seller_id.
```sql
SELECT
    order_id      STRING,
    order_item_id BIGINT,
    product_id    STRING,
    seller_id     STRING,
    price         DOUBLE,   -- item price
    freight_value DOUBLE    -- shipping cost for this item
FROM olist_order_items;
```

## olist_products
Use for: category-level analysis requiring joins. Category name is in Portuguese — always join to product_category_translation.
```sql
SELECT
    product_id               STRING,
    product_category_name    STRING,  -- Portuguese — join to translation table
    product_weight_g         BIGINT,
    product_length_cm        BIGINT,
    product_height_cm        BIGINT,
    product_width_cm         BIGINT
FROM olist_products;
```

## product_category_translation
Use for: translating Portuguese category names to English. Always join when using olist_products.
```sql
SELECT
    product_category_name          STRING,  -- Portuguese
    product_category_name_english  STRING   -- English
FROM product_category_translation;
```

## olist_order_reviews
Use for: review scores and comments at order level.
```sql
SELECT
    review_id              STRING,
    order_id               STRING,
    review_score           BIGINT,  -- 1-5
    review_comment_title   STRING,
    review_comment_message STRING,
    review_creation_date   TIMESTAMP
FROM olist_order_reviews;
```

## olist_sellers
Use for: seller location when joining raw tables.
```sql
SELECT
    seller_id             STRING,
    seller_zip_code_prefix BIGINT,
    seller_city           STRING,
    seller_state          STRING
FROM olist_sellers;
```

## olist_customers
Use for: customer location when joining raw tables (state/city breakdowns).
```sql
SELECT
    customer_id              STRING,
    customer_unique_id       STRING,
    customer_zip_code_prefix BIGINT,
    customer_city            STRING,
    customer_state           STRING   -- 2-letter Brazilian state code
FROM olist_customers;
```

## olist_order_payments
Use for: payment method analysis, installment analysis, payment value breakdown.
```sql
SELECT
    order_id              STRING,
    payment_sequential    BIGINT,
    payment_type          STRING,   -- credit_card | boleto | voucher | debit_card
    payment_installments  BIGINT,
    payment_value         DOUBLE
FROM olist_order_payments;
```

---

## Critical column rules
- vw_monthly_revenue revenue = total_revenue (NOT order_revenue)
- vw_product_metrics price = avg_price (NOT avg_order_price)
- vw_orders_metrics revenue = order_revenue (NOT total_revenue)
- delivery_days only in vw_orders_metrics
- category (English) only in vw_product_metrics
- For raw category names: join olist_products → product_category_translation on product_category_name
- seller_id in both vw_seller_metrics (aggregated) and olist_order_items (raw)
- olist_orders has NO order_date column — use order_purchase_timestamp (TIMESTAMP) for date arithmetic on raw tables, or join vw_orders_metrics to get the pre-cast order_date DATE
- olist_order_payments has NO customer_id column — to get customer_id join to olist_orders first
- customer_id in olist_orders is per-ORDER not per-PERSON — a returning customer gets a NEW customer_id each order. ALWAYS join olist_customers and use customer_unique_id to identify repeat customers. GROUP BY o.customer_id for repeat analysis will give every count=1

## Raw table join patterns

### Category cancellations:
```sql
-- COUNT(DISTINCT o.order_id) is required — the join through olist_order_items
-- produces one row per order-item, so COUNT(*) would count items not orders.
SELECT t.product_category_name_english AS category,
    COUNT(DISTINCT CASE WHEN o.order_status = 'canceled' THEN o.order_id END) AS canceled_orders,
    COUNT(DISTINCT o.order_id) AS total_orders,
    ROUND(COUNT(DISTINCT CASE WHEN o.order_status = 'canceled' THEN o.order_id END) * 100.0 / COUNT(DISTINCT o.order_id), 2) AS cancel_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
ORDER BY cancel_pct DESC LIMIT 10;
```

### Repeat-customer analysis (MUST use customer_unique_id):
```sql
-- customer_id in olist_orders is per-order. Use customer_unique_id from
-- olist_customers to track the same person across multiple orders.
WITH customer_orders AS (
    SELECT c.customer_unique_id,
        COUNT(DISTINCT o.order_id)              AS order_count,
        MIN(o.order_purchase_timestamp)         AS first_order_ts,
        MAX(o.order_purchase_timestamp)         AS last_order_ts
    FROM olist_orders o
    JOIN olist_customers c ON o.customer_id = c.customer_id
    GROUP BY c.customer_unique_id
)
SELECT order_count, COUNT(*) AS num_customers
FROM customer_orders
GROUP BY order_count
ORDER BY order_count LIMIT 10;
```

### Customer-level payment analysis (olist_order_payments has NO customer_id):
```sql
-- Bring customer_id in via olist_orders, then aggregate payment behaviour.
WITH payment_orders AS (
    SELECT o.customer_id,
        op.payment_type,
        op.payment_installments,
        SUM(op.payment_value) AS order_value
    FROM olist_orders o
    JOIN olist_order_payments op ON o.order_id = op.order_id
    GROUP BY o.customer_id, op.payment_type, op.payment_installments
),
lifetime AS (
    SELECT customer_id,
        CASE WHEN payment_type = 'credit_card' AND payment_installments > 3
             THEN 'credit_multi' ELSE 'single_or_other' END AS pay_group,
        SUM(order_value) AS lifetime_gmv,
        COUNT(*) AS order_count
    FROM payment_orders
    GROUP BY customer_id, pay_group
)
SELECT pay_group,
    ROUND(AVG(lifetime_gmv), 2) AS avg_lifetime_gmv,
    ROUND(AVG(order_count), 2)  AS avg_order_count
FROM lifetime
GROUP BY pay_group LIMIT 10;
```

### Multi-view monthly anomaly detection (revenue + cancellation + TAT):
```sql
-- vw_monthly_revenue has revenue/cancellation; vw_orders_metrics has delivery_days.
-- Join them via CTEs when a single view cannot answer.
WITH monthly_rev AS (
    SELECT year, month, year_month, total_revenue, canceled_orders, total_orders,
        LAG(total_revenue, 1) OVER (ORDER BY year, month) AS prev_revenue
    FROM vw_monthly_revenue
),
monthly_tat AS (
    SELECT order_year AS year, order_month AS month,
        AVG(delivery_days) AS avg_tat
    FROM vw_orders_metrics
    WHERE delivery_days IS NOT NULL
    GROUP BY order_year, order_month
),
flags AS (
    SELECT r.year_month,
        CASE WHEN r.prev_revenue IS NOT NULL AND r.total_revenue < 0.9 * r.prev_revenue THEN 1 ELSE 0 END AS revenue_drop,
        CASE WHEN r.total_orders > 0 AND (r.canceled_orders * 100.0 / r.total_orders) > 5 THEN 1 ELSE 0 END AS high_cancel,
        CASE WHEN t.avg_tat > 20 THEN 1 ELSE 0 END AS slow_tat
    FROM monthly_rev r
    LEFT JOIN monthly_tat t ON r.year = t.year AND r.month = t.month
)
SELECT year_month,
    CASE WHEN revenue_drop = 1 THEN 'revenue_drop_>10pct_MoM' END AS flag_revenue,
    CASE WHEN high_cancel  = 1 THEN 'cancel_rate_>5pct'       END AS flag_cancel,
    CASE WHEN slow_tat     = 1 THEN 'avg_TAT_>20_days'        END AS flag_tat,
    (revenue_drop + high_cancel + slow_tat) AS total_flags
FROM flags
WHERE (revenue_drop + high_cancel + slow_tat) >= 2
ORDER BY year_month;
```

### Freight as % of price by category:
```sql
SELECT t.product_category_name_english AS category,
    ROUND(AVG(i.freight_value / NULLIF(i.price, 0)) * 100, 2) AS freight_pct
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
ORDER BY freight_pct DESC LIMIT 10;
```

### Seller performance by category:
```sql
SELECT t.product_category_name_english AS category,
    i.seller_id,
    COUNT(DISTINCT i.order_id) AS orders,
    SUM(i.price) AS revenue
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english, i.seller_id
ORDER BY revenue DESC LIMIT 20;
```

### State + review score (olist_orders has NO customer_state — use vw_orders_metrics):
```sql
-- vw_orders_metrics has both order_id and customer_state, making it the correct join base
SELECT m.customer_state,
    AVG(r.review_score) AS avg_review
FROM vw_orders_metrics m
JOIN olist_order_reviews r ON m.order_id = r.order_id
GROUP BY m.customer_state
ORDER BY avg_review DESC LIMIT 27;
```

### Seller + review score (olist_orders has NO seller_id — use olist_order_items):
```sql
SELECT i.seller_id,
    AVG(r.review_score) AS avg_review
FROM olist_order_items i
JOIN olist_order_reviews r ON i.order_id = r.order_id
GROUP BY i.seller_id
ORDER BY avg_review ASC LIMIT 20;
```

### Pareto (cumulative GMV — sellers making up first 80% of revenue):
```sql
-- NEVER use PERCENTILE_CONT for Pareto — it gives the 80th-percentile value, not cumulative share.
-- Use SUM() OVER with ROWS UNBOUNDED PRECEDING for true cumulative share.
WITH seller_gmv AS (
    SELECT seller_id, SUM(price + freight_value) AS gmv
    FROM olist_order_items
    GROUP BY seller_id
),
total AS (SELECT SUM(gmv) AS total_gmv FROM seller_gmv),
cumulative AS (
    SELECT seller_id, gmv,
        SUM(gmv) OVER (ORDER BY gmv DESC ROWS UNBOUNDED PRECEDING) AS cum_gmv
    FROM seller_gmv
),
labeled AS (
    SELECT c.seller_id, c.gmv,
        CASE WHEN c.cum_gmv - c.gmv < t.total_gmv * 0.8 THEN 'pareto_80pct' ELSE 'rest' END AS grp
    FROM cumulative c CROSS JOIN total t
)
SELECT grp,
    COUNT(DISTINCT seller_id) AS num_sellers,
    ROUND(SUM(gmv), 2) AS total_gmv
FROM labeled
GROUP BY grp
LIMIT 10;
```

### Monthly MoM growth rate and acceleration (correct formula + no window fn in WHERE):
```sql
-- Rule: store LAG() as a named column in a prior CTE — never inline it in arithmetic
-- Rule: window functions cannot appear in WHERE; filter on the CTE column instead
WITH monthly_rev AS (
    SELECT year, month, year_month, total_revenue,
        LAG(total_revenue, 1) OVER (ORDER BY year, month) AS prev_revenue
    FROM vw_monthly_revenue
),
growth_rates AS (
    SELECT year_month,
        ROUND((total_revenue - prev_revenue) * 100.0 / NULLIF(prev_revenue, 0), 2) AS mom_growth_pct
    FROM monthly_rev
    WHERE prev_revenue IS NOT NULL AND prev_revenue > 0   -- filter on CTE column, not on LAG()
),
acceleration AS (
    SELECT year_month, mom_growth_pct,
        LAG(mom_growth_pct, 1) OVER (ORDER BY year_month) AS prev_mom_growth_pct
    FROM growth_rates
)
SELECT year_month, mom_growth_pct, prev_mom_growth_pct,
    CASE WHEN prev_mom_growth_pct IS NOT NULL AND mom_growth_pct > prev_mom_growth_pct
         THEN 'accelerated' ELSE 'decelerated_or_initial' END AS trend
FROM acceleration
WHERE prev_mom_growth_pct IS NOT NULL
ORDER BY year_month
LIMIT 50;
```

### YoY quarterly growth (LAG offset=4, never 3rd CTE that drops totals):
```sql
-- Rule: LAG offset must be 4 (same quarter prior year). NEVER 1 (that's QoQ).
-- Rule: compute growth in the final SELECT, not a separate CTE — avoids dropping total_revenue/total_orders
WITH quarterly AS (
    SELECT order_year, CEIL(order_month / 3.0) AS quarter,
        SUM(order_revenue) AS total_revenue,
        COUNT(DISTINCT order_id) AS total_orders
    FROM vw_orders_metrics
    GROUP BY order_year, CEIL(order_month / 3.0)
),
lagged AS (
    SELECT order_year, quarter, total_revenue, total_orders,
        LAG(total_revenue, 4) OVER (ORDER BY order_year, quarter) AS prev_revenue,
        LAG(total_orders, 4)  OVER (ORDER BY order_year, quarter) AS prev_orders
    FROM quarterly
)
SELECT order_year, quarter, total_revenue, total_orders,
    ROUND((total_revenue - prev_revenue) * 100.0 / NULLIF(prev_revenue, 0), 2) AS rev_yoy_growth_pct,
    ROUND((total_orders  - prev_orders)  * 100.0 / NULLIF(prev_orders,  0), 2) AS order_yoy_growth_pct
FROM lagged
WHERE prev_revenue IS NOT NULL
ORDER BY order_year, quarter
LIMIT 20;
```

### Seller quartile → categories (multi-CTE + final JOIN for category name):
```sql
-- Rule: final SELECT must include its own JOINs to get English category names
-- t alias only exists if explicitly joined in the final SELECT — CTE aliases do not propagate
WITH seller_orders AS (
    SELECT seller_id, COUNT(DISTINCT order_id) AS order_count
    FROM olist_order_items GROUP BY seller_id
),
seller_reviews AS (
    SELECT i.seller_id, AVG(r.review_score) AS avg_review_score
    FROM olist_order_items i JOIN olist_order_reviews r ON i.order_id = r.order_id
    GROUP BY i.seller_id
),
ranked AS (
    SELECT o.seller_id,
        PERCENT_RANK() OVER (ORDER BY o.order_count ASC)         AS order_rank,  -- top quartile >= 0.75
        PERCENT_RANK() OVER (ORDER BY r.avg_review_score ASC)    AS review_rank  -- bottom quartile <= 0.25
    FROM seller_orders o JOIN seller_reviews r ON o.seller_id = r.seller_id
    WHERE r.avg_review_score IS NOT NULL
)
SELECT t.product_category_name_english AS category,
    COUNT(DISTINCT rs.seller_id) AS num_sellers
FROM ranked rs                               -- always alias ranked_sellers as rs (not r)
JOIN olist_order_items i   ON rs.seller_id = i.seller_id
JOIN olist_products p      ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE rs.order_rank >= 0.75 AND rs.review_rank <= 0.25
GROUP BY t.product_category_name_english
ORDER BY num_sellers DESC LIMIT 20;
```

### State above-avg revenue AND below-avg TAT (separate CTEs for revenue/TAT vs reviews):
```sql
-- Rule: "above-average revenue" for states = SUM(order_revenue) per state vs AVG of those totals
-- Using AVG(order_revenue) gives 0 results: high-total-revenue states (SP, RJ, MG) have MANY
-- small orders → low per-order avg, but high total revenue. Always use SUM for market size.
-- Rule: compute revenue/TAT from vw_orders_metrics ALONE — no review join needed here
WITH state_totals AS (
    SELECT customer_state,
        SUM(order_revenue) AS total_rev,
        AVG(delivery_days) AS avg_tat
    FROM vw_orders_metrics WHERE delivery_days IS NOT NULL GROUP BY customer_state
),
platform_avg AS (
    SELECT AVG(total_rev) AS avg_rev, AVG(avg_tat) AS avg_tat FROM state_totals
),
state_reviews AS (
    SELECT m.customer_state, AVG(r.review_score) AS avg_review_score
    FROM vw_orders_metrics m JOIN olist_order_reviews r ON m.order_id = r.order_id
    GROUP BY m.customer_state
),
platform_review AS (
    SELECT AVG(r.review_score) AS avg_review_score
    FROM vw_orders_metrics m JOIN olist_order_reviews r ON m.order_id = r.order_id
)
SELECT st.customer_state, st.total_rev, st.avg_tat, sr.avg_review_score,
    pa.avg_rev AS platform_avg_rev, pa.avg_tat AS platform_avg_tat,
    pr.avg_review_score AS platform_avg_review_score
FROM state_totals st
JOIN state_reviews sr ON st.customer_state = sr.customer_state
CROSS JOIN platform_avg pa
CROSS JOIN platform_review pr
WHERE st.total_rev > pa.avg_rev AND st.avg_tat < pa.avg_tat
ORDER BY sr.avg_review_score DESC LIMIT 27;
```

### Pareto + review scores per group (GROUP BY grp only — NOT by avg_review_score):
```sql
-- Rule: GROUP BY l.grp only — adding sr.avg_review_score to GROUP BY turns each seller
-- into its own group, producing hundreds of rows instead of 2 summary rows
WITH seller_gmv AS (
    SELECT seller_id, SUM(price + freight_value) AS gmv FROM olist_order_items GROUP BY seller_id
),
total AS (SELECT SUM(gmv) AS total_gmv FROM seller_gmv),
cumulative AS (
    SELECT seller_id, gmv,
        SUM(gmv) OVER (ORDER BY gmv DESC ROWS UNBOUNDED PRECEDING) AS cum_gmv
    FROM seller_gmv
),
labeled AS (
    SELECT c.seller_id, c.gmv,
        CASE WHEN c.cum_gmv - c.gmv < t.total_gmv * 0.8 THEN 'pareto_80pct' ELSE 'rest' END AS grp
    FROM cumulative c CROSS JOIN total t
),
seller_reviews AS (
    SELECT i.seller_id, AVG(r.review_score) AS avg_review_score
    FROM olist_order_items i JOIN olist_order_reviews r ON i.order_id = r.order_id
    GROUP BY i.seller_id
)
SELECT l.grp,
    COUNT(DISTINCT l.seller_id) AS num_sellers,
    ROUND(SUM(l.gmv), 2) AS total_gmv,
    ROUND(AVG(sr.avg_review_score), 3) AS avg_review_score   -- AVG across sellers, not per seller
FROM labeled l
LEFT JOIN seller_reviews sr ON l.seller_id = sr.seller_id
GROUP BY l.grp   -- ONLY group by l.grp — never include sr.avg_review_score in GROUP BY
ORDER BY num_sellers DESC LIMIT 10;
```

### Category risk matrix (cancel_rate + avg TAT + review in one pass — no status filter):
```sql
-- Rule: NEVER add WHERE order_status='delivered' — it zeros out cancel_rate
-- Rule: tat_risk ORDER BY avg_tat_days ASC (longer TAT = rank 1) — NEVER DESC
-- Rule: review_risk ORDER BY avg_review_score DESC (lower review = rank 1) — use directly, NEVER (1-review_risk)
-- Formula weights: cancel 0.4 + review 0.4 + tat 0.2 = 1.0 exactly
-- Use CASE WHEN inside aggregates to handle both metrics from all orders in one pass
WITH category_metrics AS (
    SELECT t.product_category_name_english AS category,
        COUNT(DISTINCT o.order_id) AS total_orders,
        ROUND(COUNT(DISTINCT CASE WHEN o.order_status = 'canceled' THEN o.order_id END)
              * 100.0 / NULLIF(COUNT(DISTINCT o.order_id), 0), 2) AS cancel_rate,
        ROUND(AVG(r.review_score), 2) AS avg_review_score,
        ROUND(AVG(CASE WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
                       THEN DATEDIFF(o.order_delivered_customer_date, o.order_purchase_timestamp)
                  END), 2) AS avg_tat_days
    FROM olist_orders o
    JOIN olist_order_items i ON o.order_id = i.order_id
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    LEFT JOIN olist_order_reviews r ON o.order_id = r.order_id
    GROUP BY t.product_category_name_english
    HAVING COUNT(DISTINCT o.order_id) >= 10
),
risk_scores AS (
    SELECT *,
        PERCENT_RANK() OVER (ORDER BY cancel_rate ASC)       AS cancel_risk,   -- higher cancel = rank 1
        PERCENT_RANK() OVER (ORDER BY avg_review_score DESC) AS review_risk,   -- lower review = rank 1
        PERCENT_RANK() OVER (ORDER BY avg_tat_days ASC)      AS tat_risk       -- longer TAT = rank 1
    FROM category_metrics
    WHERE avg_tat_days IS NOT NULL
)
SELECT category, total_orders, cancel_rate, avg_review_score, avg_tat_days,
    ROUND((cancel_risk * 0.4 + review_risk * 0.4 + tat_risk * 0.2), 3) AS risk_score
FROM risk_scores
ORDER BY risk_score DESC
LIMIT 20;
```

---

## Advanced SQL Patterns

### Timestamp rules for raw tables:
```
-- YEAR() / MONTH() work on TIMESTAMP and DATE in Databricks
-- DATE_FORMAT(ts, 'yyyy-MM') produces year_month strings e.g. '2017-01'
-- olist_orders.order_purchase_timestamp is TIMESTAMP — use DATE_FORMAT for year_month grouping
-- NEVER use order_date on raw tables — only vw_orders_metrics has order_date (pre-cast DATE)
-- For delivery days on raw tables: DATEDIFF(order_delivered_customer_date, order_purchase_timestamp)
-- Always filter WHERE order_delivered_customer_date IS NOT NULL when computing delivery days on raw tables
```

### Payment deduplication rule:
```
-- olist_order_payments has one row per payment method per order
-- Orders paid with multiple methods (e.g. voucher + credit card) produce multiple rows
-- Filter WHERE payment_sequential = 1 to get the primary payment method only
-- Use SUM(payment_value) across all payment_sequential values for the true order total
-- NEVER COUNT(order_id) on olist_order_payments without deduplication — it inflates counts
```

### Category + time revenue (foundation for waterfall, stacked area, bump chart, heatmap):
```sql
-- Rule: NO view covers category + time together — always use raw tables for this pattern
-- Rule: filter NOT IN ('canceled','unavailable') to match revenue semantics of the views
-- Rule: use LIMIT 500 for multi-category × multi-month queries (10 categories × 24 months = 240 rows)
SELECT
    t.product_category_name_english AS category,
    YEAR(o.order_purchase_timestamp)                        AS year,
    MONTH(o.order_purchase_timestamp)                       AS month,
    DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM')      AS year_month,
    COUNT(DISTINCT o.order_id)                              AS total_orders,
    ROUND(SUM(i.price), 2)                                  AS total_revenue
FROM olist_orders o
JOIN olist_order_items i          ON o.order_id   = i.order_id
JOIN olist_products p             ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE o.order_status NOT IN ('canceled', 'unavailable')
GROUP BY t.product_category_name_english,
         YEAR(o.order_purchase_timestamp),
         MONTH(o.order_purchase_timestamp),
         DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM')
ORDER BY year, month, total_revenue DESC
LIMIT 500;
```

### Waterfall: category revenue change between two years (2017 vs 2018):
```sql
-- Rule: FULL OUTER JOIN the two year CTEs — categories may appear in one year but not the other
-- Rule: ORDER BY ABS(delta) DESC to surface biggest movers at the top
-- Rule: delta can be negative (shrinking categories) — essential for waterfall chart
WITH cat_2017 AS (
    SELECT t.product_category_name_english AS category,
           ROUND(SUM(i.price), 2) AS revenue_2017
    FROM olist_orders o
    JOIN olist_order_items i          ON o.order_id   = i.order_id
    JOIN olist_products p             ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE YEAR(o.order_purchase_timestamp) = 2017
      AND o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY t.product_category_name_english
),
cat_2018 AS (
    SELECT t.product_category_name_english AS category,
           ROUND(SUM(i.price), 2) AS revenue_2018
    FROM olist_orders o
    JOIN olist_order_items i          ON o.order_id   = i.order_id
    JOIN olist_products p             ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE YEAR(o.order_purchase_timestamp) = 2018
      AND o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY t.product_category_name_english
)
SELECT
    COALESCE(a.category, b.category)        AS category,
    COALESCE(a.revenue_2017, 0)             AS revenue_2017,
    COALESCE(b.revenue_2018, 0)             AS revenue_2018,
    ROUND(COALESCE(b.revenue_2018, 0) - COALESCE(a.revenue_2017, 0), 2) AS delta
FROM cat_2017 a
FULL OUTER JOIN cat_2018 b ON a.category = b.category
ORDER BY ABS(COALESCE(b.revenue_2018, 0) - COALESCE(a.revenue_2017, 0)) DESC
LIMIT 20;
```

### Customer state → seller state flow (Sankey / flow analysis):
```sql
-- Rule: olist_orders has NO seller_id and NO customer_state — need all 4 tables
-- Rule: olist_orders has NO customer_state → join olist_customers for customer_state
-- Rule: seller_state comes from olist_sellers, reached via olist_order_items.seller_id
-- Rule: COUNT(DISTINCT o.order_id) — the join produces one row per order-item pair
-- Rule: use CTEs to pre-filter top-N states before joining — avoids full cross-product
-- Rule: filter top customer states and top seller states separately, then join
WITH top_customer_states AS (
    SELECT c.customer_state
    FROM olist_orders o
    JOIN olist_customers c ON o.customer_id = c.customer_id
    GROUP BY c.customer_state
    ORDER BY COUNT(DISTINCT o.order_id) DESC
    LIMIT 8
),
top_seller_states AS (
    SELECT s.seller_state
    FROM olist_order_items i
    JOIN olist_sellers s ON i.seller_id = s.seller_id
    GROUP BY s.seller_state
    ORDER BY COUNT(DISTINCT i.order_id) DESC
    LIMIT 8
)
SELECT
    c.customer_state,
    s.seller_state,
    COUNT(DISTINCT o.order_id)  AS order_count,
    ROUND(SUM(i.price), 2)      AS total_revenue
FROM olist_orders o
JOIN olist_customers c   ON o.customer_id = c.customer_id
JOIN olist_order_items i ON o.order_id    = i.order_id
JOIN olist_sellers s     ON i.seller_id   = s.seller_id
WHERE o.order_status = 'delivered'
  AND c.customer_state IN (SELECT customer_state FROM top_customer_states)
  AND s.seller_state   IN (SELECT seller_state   FROM top_seller_states)
GROUP BY c.customer_state, s.seller_state
ORDER BY order_count DESC
LIMIT 100;
```

### Category rank over time — bump chart (rank shifts month by month):
```sql
-- Rule: use RANK() PARTITION BY year_month — gives rank within each month independently
-- Rule: top_categories CTE limits to top N by total GMV so chart stays readable
-- Rule: LIMIT 500 — 10 categories × ~24 months = ~240 rows needed
WITH monthly_cat AS (
    SELECT
        t.product_category_name_english                     AS category,
        DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM')  AS year_month,
        ROUND(SUM(i.price), 2)                              AS monthly_revenue
    FROM olist_orders o
    JOIN olist_order_items i          ON o.order_id   = i.order_id
    JOIN olist_products p             ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY t.product_category_name_english,
             DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM')
),
top_cats AS (
    SELECT category
    FROM monthly_cat
    GROUP BY category
    ORDER BY SUM(monthly_revenue) DESC
    LIMIT 10
),
ranked AS (
    SELECT m.category, m.year_month, m.monthly_revenue,
           RANK() OVER (PARTITION BY m.year_month ORDER BY m.monthly_revenue DESC) AS revenue_rank
    FROM monthly_cat m
    WHERE m.category IN (SELECT category FROM top_cats)
)
SELECT category, year_month, monthly_revenue, revenue_rank
FROM ranked
ORDER BY year_month, revenue_rank
LIMIT 500;
```

### Delivery days distribution by category — individual rows for violin / box plot:
```sql
-- Rule: vw_orders_metrics has delivery_days (pre-computed); join to raw tables for category
-- Rule: use LIMIT 500 — individual rows needed for distribution charts, not aggregates
-- Rule: top 10 categories by order volume filtered via subquery to keep chart readable
WITH top_cats AS (
    SELECT category FROM vw_product_metrics
    ORDER BY total_orders DESC LIMIT 10
)
SELECT
    t.product_category_name_english AS category,
    m.delivery_days
FROM vw_orders_metrics m
JOIN olist_order_items i          ON m.order_id   = i.order_id
JOIN olist_products p             ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE m.delivery_days IS NOT NULL
  AND m.order_status = 'delivered'
  AND t.product_category_name_english IN (SELECT category FROM top_cats)
ORDER BY category
LIMIT 500;
```

### Payment type mix by year — stacked bar (% share per year):
```sql
-- Rule: payment_sequential = 1 → primary payment method only, avoids double-counting
-- Rule: window SUM() OVER (PARTITION BY year) gives yearly total for % calculation
-- Rule: YEAR(order_purchase_timestamp) on raw olist_orders for the year dimension
WITH payment_year AS (
    SELECT
        YEAR(o.order_purchase_timestamp)    AS year,
        op.payment_type,
        COUNT(DISTINCT o.order_id)          AS order_count,
        ROUND(SUM(op.payment_value), 2)     AS total_value
    FROM olist_orders o
    JOIN olist_order_payments op ON o.order_id = op.order_id
    WHERE op.payment_sequential = 1
      AND o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY YEAR(o.order_purchase_timestamp), op.payment_type
)
SELECT
    year,
    payment_type,
    order_count,
    total_value,
    ROUND(order_count * 100.0 / SUM(order_count) OVER (PARTITION BY year), 2) AS pct_of_year
FROM payment_year
ORDER BY year, order_count DESC
LIMIT 50;
```

### Customer state × payment type — heatmap matrix:
```sql
-- Rule: olist_order_payments has NO customer_id — join via olist_orders, then olist_customers
-- Rule: payment_sequential = 1 to count each order once under its primary payment method
SELECT
    c.customer_state,
    op.payment_type,
    COUNT(DISTINCT o.order_id)      AS order_count,
    ROUND(SUM(op.payment_value), 2) AS total_value
FROM olist_orders o
JOIN olist_customers c          ON o.customer_id = c.customer_id
JOIN olist_order_payments op    ON o.order_id    = op.order_id
WHERE op.payment_sequential = 1
  AND o.order_status NOT IN ('canceled', 'unavailable')
GROUP BY c.customer_state, op.payment_type
ORDER BY order_count DESC
LIMIT 200;
```

### Payment installments distribution by category — violin / heatmap:
```sql
-- Rule: installment analysis needs olist_order_payments + olist_orders + items + products + translation
-- Rule: filter payment_type = 'credit_card' — installments only meaningful for credit
-- Rule: LIMIT 500 for individual-row distribution charts
SELECT
    t.product_category_name_english AS category,
    op.payment_installments,
    COUNT(DISTINCT o.order_id)      AS order_count
FROM olist_orders o
JOIN olist_order_payments op      ON o.order_id   = op.order_id
JOIN olist_order_items i          ON o.order_id   = i.order_id
JOIN olist_products p             ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE op.payment_type = 'credit_card'
  AND o.order_status NOT IN ('canceled', 'unavailable')
GROUP BY t.product_category_name_english, op.payment_installments
ORDER BY category, payment_installments
LIMIT 500;
```

### Multi-metric per category — correlation / bubble / scatter matrix:
```sql
-- Rule: four separate CTEs, each responsible for ONE metric dimension
-- Rule: JOIN all CTEs on category — only categories appearing in all dimensions are included
-- Rule: HAVING total_orders >= 50 removes noisy low-volume categories
-- Produces: category × (revenue, orders, avg_price, freight_pct, delivery_days, review, cancel_rate)
WITH cat_base AS (
    SELECT
        t.product_category_name_english                                         AS category,
        COUNT(DISTINCT o.order_id)                                              AS total_orders,
        ROUND(SUM(i.price), 2)                                                  AS total_revenue,
        ROUND(AVG(i.price), 2)                                                  AS avg_price,
        ROUND(AVG(i.freight_value / NULLIF(i.price, 0)) * 100, 2)              AS freight_pct,
        ROUND(COUNT(DISTINCT CASE WHEN o.order_status = 'canceled'
              THEN o.order_id END) * 100.0 / NULLIF(COUNT(DISTINCT o.order_id), 0), 2) AS cancel_rate
    FROM olist_orders o
    JOIN olist_order_items i          ON o.order_id   = i.order_id
    JOIN olist_products p             ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    GROUP BY t.product_category_name_english
    HAVING COUNT(DISTINCT o.order_id) >= 50
),
cat_delivery AS (
    SELECT
        t.product_category_name_english AS category,
        ROUND(AVG(m.delivery_days), 1)  AS avg_delivery_days
    FROM vw_orders_metrics m
    JOIN olist_order_items i          ON m.order_id   = i.order_id
    JOIN olist_products p             ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE m.delivery_days IS NOT NULL AND m.order_status = 'delivered'
    GROUP BY t.product_category_name_english
),
cat_reviews AS (
    SELECT
        t.product_category_name_english AS category,
        ROUND(AVG(r.review_score), 3)   AS avg_review_score
    FROM olist_order_items i
    JOIN olist_products p             ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    JOIN olist_order_reviews r        ON i.order_id   = r.order_id
    GROUP BY t.product_category_name_english
)
SELECT
    b.category, b.total_orders, b.total_revenue, b.avg_price,
    b.freight_pct, b.cancel_rate,
    d.avg_delivery_days,
    r.avg_review_score
FROM cat_base b
JOIN cat_delivery d ON b.category = d.category
JOIN cat_reviews  r ON b.category = r.category
ORDER BY b.total_revenue DESC
LIMIT 100;
```

### State bubble chart — revenue + delivery + review per state:
```sql
-- Rule: three dimensions per state for bubble chart (size=revenue, x=delivery, y=review)
-- Rule: state review joins via vw_orders_metrics (has order_id + customer_state) → olist_order_reviews
-- Rule: CROSS JOIN platform_avg to include benchmark lines in the output
WITH state_metrics AS (
    SELECT
        m.customer_state,
        ROUND(SUM(m.order_revenue), 2)  AS total_revenue,
        ROUND(AVG(m.delivery_days), 1)  AS avg_delivery_days,
        COUNT(DISTINCT m.order_id)      AS total_orders
    FROM vw_orders_metrics m
    WHERE m.delivery_days IS NOT NULL
    GROUP BY m.customer_state
),
state_reviews AS (
    SELECT
        m.customer_state,
        ROUND(AVG(r.review_score), 3) AS avg_review_score
    FROM vw_orders_metrics m
    JOIN olist_order_reviews r ON m.order_id = r.order_id
    GROUP BY m.customer_state
),
platform AS (
    SELECT
        ROUND(AVG(sm.avg_delivery_days), 1) AS platform_avg_delivery,
        ROUND(AVG(sr.avg_review_score), 3)  AS platform_avg_review
    FROM state_metrics sm
    JOIN state_reviews sr ON sm.customer_state = sr.customer_state
)
SELECT
    sm.customer_state,
    sm.total_revenue,
    sm.total_orders,
    sm.avg_delivery_days,
    sr.avg_review_score,
    p.platform_avg_delivery,
    p.platform_avg_review
FROM state_metrics sm
JOIN state_reviews  sr ON sm.customer_state = sr.customer_state
CROSS JOIN platform p
ORDER BY sm.total_revenue DESC
LIMIT 50;
```

### Customer cohort retention — cohort heatmap:
```sql
-- Rule: use customer_unique_id (not customer_id) — customer_id is per-order
-- Rule: cohort_month = DATE_FORMAT of the customer's FIRST order
-- Rule: period_number = months between cohort_month and order_month (0 = acquisition month)
-- Rule: MONTHS_BETWEEN(date2, date1) in Databricks — note argument order
-- Rule: LIMIT 500 — cohort × period matrix can have many rows
WITH first_orders AS (
    SELECT
        c.customer_unique_id,
        DATE_FORMAT(MIN(o.order_purchase_timestamp), 'yyyy-MM') AS cohort_month
    FROM olist_orders o
    JOIN olist_customers c ON o.customer_id = c.customer_id
    GROUP BY c.customer_unique_id
),
all_orders AS (
    SELECT
        c.customer_unique_id,
        DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM') AS order_month
    FROM olist_orders o
    JOIN olist_customers c ON o.customer_id = c.customer_id
),
cohort_activity AS (
    SELECT
        f.cohort_month,
        a.order_month,
        COUNT(DISTINCT f.customer_unique_id)   AS active_customers,
        CAST(MONTHS_BETWEEN(
            TO_DATE(a.order_month,  'yyyy-MM'),
            TO_DATE(f.cohort_month, 'yyyy-MM')
        ) AS INT)                              AS period_number
    FROM first_orders f
    JOIN all_orders a ON f.customer_unique_id = a.customer_unique_id
    WHERE a.order_month >= f.cohort_month
    GROUP BY f.cohort_month, a.order_month
),
cohort_sizes AS (
    SELECT cohort_month, COUNT(DISTINCT customer_unique_id) AS cohort_size
    FROM first_orders
    GROUP BY cohort_month
)
SELECT
    ca.cohort_month,
    cs.cohort_size,
    ca.period_number,
    ca.active_customers,
    ROUND(ca.active_customers * 100.0 / cs.cohort_size, 2) AS retention_pct
FROM cohort_activity ca
JOIN cohort_sizes cs ON ca.cohort_month = cs.cohort_month
ORDER BY ca.cohort_month, ca.period_number
LIMIT 500;
```

### RFM customer segmentation — treemap / bubble by segment:
```sql
-- Rule: recency reference date = '2018-09-01' (last full data month in Olist)
-- Rule: NTILE(5) — 5 = best; for recency, ORDER BY recency_days ASC (fewer days = more recent = score 5)
-- Rule: assign segment label in rfm_scores CTE, then GROUP BY segment label string (not repeated CASE WHEN)
-- Rule: use customer_unique_id (not customer_id) for person-level aggregation
WITH customer_metrics AS (
    SELECT
        c.customer_unique_id,
        DATEDIFF(TO_DATE('2018-09-01'), MAX(DATE(o.order_purchase_timestamp))) AS recency_days,
        COUNT(DISTINCT o.order_id)                                              AS frequency,
        ROUND(SUM(i.price + i.freight_value), 2)                               AS monetary
    FROM olist_orders o
    JOIN olist_customers c   ON o.customer_id = c.customer_id
    JOIN olist_order_items i ON o.order_id    = i.order_id
    WHERE o.order_status = 'delivered'
    GROUP BY c.customer_unique_id
),
rfm_scores AS (
    SELECT *,
        NTILE(5) OVER (ORDER BY recency_days ASC) AS r_score,
        NTILE(5) OVER (ORDER BY frequency ASC)    AS f_score,
        NTILE(5) OVER (ORDER BY monetary ASC)     AS m_score
    FROM customer_metrics
),
rfm_labeled AS (
    SELECT *,
        CASE
            WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'Champions'
            WHEN r_score >= 3 AND f_score >= 3                  THEN 'Loyal'
            WHEN r_score >= 4 AND f_score <= 2                  THEN 'New Customers'
            WHEN r_score <= 2 AND f_score >= 3                  THEN 'At Risk'
            WHEN r_score <= 2 AND f_score <= 2                  THEN 'Lost'
            ELSE 'Potential Loyalists'
        END AS rfm_segment
    FROM rfm_scores
)
SELECT
    rfm_segment,
    COUNT(*)                    AS customer_count,
    ROUND(AVG(recency_days), 1) AS avg_recency_days,
    ROUND(AVG(frequency), 2)    AS avg_frequency,
    ROUND(AVG(monetary), 2)     AS avg_monetary
FROM rfm_labeled
GROUP BY rfm_segment
ORDER BY customer_count DESC
LIMIT 20;
```

### Order value histogram — bucket distribution:
```sql
-- Rule: CASE WHEN bucket in GROUP BY must exactly match SELECT CASE WHEN — no aliases in GROUP BY
-- Rule: ORDER BY MIN(order_revenue) gives natural numeric bucket order (not alphabetical)
SELECT
    CASE
        WHEN order_revenue < 50   THEN '1. 0–50'
        WHEN order_revenue < 100  THEN '2. 50–100'
        WHEN order_revenue < 200  THEN '3. 100–200'
        WHEN order_revenue < 500  THEN '4. 200–500'
        WHEN order_revenue < 1000 THEN '5. 500–1000'
        ELSE                           '6. 1000+'
    END                            AS revenue_bucket,
    COUNT(*)                       AS order_count,
    ROUND(AVG(order_revenue), 2)   AS avg_order_value,
    ROUND(MIN(order_revenue), 2)   AS bucket_min
FROM vw_orders_metrics
WHERE order_revenue IS NOT NULL AND order_revenue > 0
GROUP BY
    CASE
        WHEN order_revenue < 50   THEN '1. 0–50'
        WHEN order_revenue < 100  THEN '2. 50–100'
        WHEN order_revenue < 200  THEN '3. 100–200'
        WHEN order_revenue < 500  THEN '4. 200–500'
        WHEN order_revenue < 1000 THEN '5. 500–1000'
        ELSE                           '6. 1000+'
    END
ORDER BY bucket_min
LIMIT 10;
```

### Seller region × customer region delivery heatmap:
```sql
-- Rule: 4-table join — seller_state via olist_sellers (reached through olist_order_items)
-- Rule: customer_state via olist_customers (reached through olist_orders.customer_id)
-- Rule: delivery_days from vw_orders_metrics (has order_id + delivery_days pre-computed)
SELECT
    c.customer_state,
    s.seller_state,
    ROUND(AVG(m.delivery_days), 1)  AS avg_delivery_days,
    COUNT(DISTINCT m.order_id)      AS order_count
FROM vw_orders_metrics m
JOIN olist_order_items i  ON m.order_id   = i.order_id
JOIN olist_sellers s      ON i.seller_id  = s.seller_id
JOIN olist_customers c    ON m.customer_id = c.customer_id
WHERE m.delivery_days IS NOT NULL
  AND m.order_status = 'delivered'
GROUP BY c.customer_state, s.seller_state
HAVING COUNT(DISTINCT m.order_id) >= 20
ORDER BY avg_delivery_days DESC
LIMIT 200;
```
