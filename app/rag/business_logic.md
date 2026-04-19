# Olist Business Logic

> IMPORTANT: This document contains business context and SQL patterns ONLY.
> "business_logic" is NOT a table or view in Databricks.
> NEVER use business_logic in any SQL query.
> Only use: vw_orders_metrics, vw_seller_metrics, vw_product_metrics, vw_monthly_revenue
> OR raw tables: olist_orders, olist_order_items, olist_products, product_category_translation, olist_order_reviews, olist_sellers

## Dataset Context
- Platform: Olist Brazilian e-commerce marketplace
- Date range: September 2016 to October 2018
- Currency: Brazilian Real (R$)
- Total delivered orders: ~96,478 | Canceled: 625 | Shipped: 1,107
- Avg order value: R$137.75 | Min: R$0.85 | Max: R$13,440
- Largest market: SP (41,746 orders), RJ (12,852), MG (11,635)
- Top categories: bed_bath_table, health_beauty, sports_leisure

## How the Marketplace Works
- Independent sellers list products on the Olist platform
- Customers place orders → seller ships directly to customer
- Olist handles payments and reviews

## Order Lifecycle
1. created → approved → invoiced → processing → shipped → delivered
2. canceled — can happen at any stage
3. unavailable — product became unavailable

## Revenue Recognition
- Revenue = sum of item prices (order_revenue) — excludes freight
- GMV = revenue + freight (order_total)
- "revenue" or "sales" → use order_revenue
- "total transaction value" → use order_total

## Geographic Context
- All locations are in Brazil. State codes are 2-letter Brazilian states.
- SP = São Paulo (largest), RJ = Rio de Janeiro, MG = Minas Gerais
- Remote states (AM, RR, AC, AP) → longer delivery times

## Delivery Performance
- Good: under 10 days | Average: ~12 days | Slow: over 20 days
- delivery_days is NULL for undelivered orders — always filter WHERE delivery_days IS NOT NULL

## Review Scores
- Scale 1–5. Platform average ~4.0
- Below 3.0 = seller quality issue | Below 3.5 = category quality concern

## Anomaly Thresholds
- Revenue drop > 10% MoM = anomaly
- Cancellation rate > 5% = anomaly
- Avg delivery days > 20 = delivery issue
- Seller review < 3.0 = quality issue
- Category review < 3.5 = quality concern

---

## SQL Pattern Library

### PATTERN 1 — Simple aggregation (use views)
Question: "What were total orders and revenue in 2018?"
```sql
SELECT COUNT(DISTINCT order_id) AS total_orders,
       ROUND(SUM(order_revenue), 2) AS total_revenue
FROM vw_orders_metrics
WHERE order_year = 2018
LIMIT 1
```

### PATTERN 2 — Ranking with HAVING filter
Question: "Which sellers have more than 100 orders but average review score below 3?"
```sql
SELECT seller_id, seller_state, total_orders, ROUND(avg_review_score, 2) AS avg_score
FROM vw_seller_metrics
WHERE total_orders > 100 AND avg_review_score < 3.0
ORDER BY avg_review_score ASC
LIMIT 20
```

### PATTERN 3 — Month over month with LAG window function
Question: "What is the month over month revenue change for 2018?"
```sql
WITH monthly AS (
    SELECT year, month, year_month, total_revenue,
           LAG(total_revenue, 1) OVER (ORDER BY year, month) AS prev_revenue
    FROM vw_monthly_revenue
    WHERE year = 2018
)
SELECT year_month,
       ROUND(total_revenue, 2) AS revenue,
       ROUND(prev_revenue, 2) AS prev_month_revenue,
       ROUND((total_revenue - prev_revenue) / NULLIF(prev_revenue, 0) * 100, 2) AS pct_change
FROM monthly
ORDER BY year, month
LIMIT 12
```

### PATTERN 4 — Year over year comparison
Question: "Compare total revenue between 2017 and 2018"
```sql
SELECT year,
       SUM(total_revenue) AS annual_revenue,
       SUM(total_orders) AS annual_orders
FROM vw_monthly_revenue
WHERE year IN (2017, 2018)
GROUP BY year
ORDER BY year
LIMIT 2
```

### PATTERN 5 — Running total / cumulative
Question: "Show me cumulative revenue by month for 2017"
```sql
SELECT year_month,
       ROUND(total_revenue, 2) AS monthly_revenue,
       ROUND(SUM(total_revenue) OVER (PARTITION BY year ORDER BY month), 2) AS cumulative_revenue
FROM vw_monthly_revenue
WHERE year = 2017
ORDER BY month
LIMIT 12
```

### PATTERN 6 — Percentile / top N percent
Question: "Who are the top 10% of sellers by revenue?"
```sql
WITH ranked AS (
    SELECT seller_id, seller_state, total_revenue,
           NTILE(10) OVER (ORDER BY total_revenue DESC) AS decile
    FROM vw_seller_metrics
)
SELECT seller_id, seller_state, ROUND(total_revenue, 2) AS revenue
FROM ranked
WHERE decile = 1
ORDER BY total_revenue DESC
LIMIT 50
```

### PATTERN 7 — Above/below average comparison
Question: "Which sellers perform above average in both revenue and review score?"
```sql
WITH averages AS (
    SELECT AVG(total_revenue) AS avg_rev,
           AVG(avg_review_score) AS avg_score
    FROM vw_seller_metrics
)
SELECT s.seller_id, s.seller_state,
       ROUND(s.total_revenue, 2) AS revenue,
       ROUND(s.avg_review_score, 2) AS review_score
FROM vw_seller_metrics s
CROSS JOIN averages a
WHERE s.total_revenue > a.avg_rev
  AND s.avg_review_score > a.avg_score
ORDER BY s.total_revenue DESC
LIMIT 20
```

### PATTERN 8 — Delivery time by state (geographic analysis)
Question: "Which states have the worst average delivery time?"
```sql
SELECT customer_state,
       ROUND(AVG(delivery_days), 1) AS avg_delivery_days,
       COUNT(*) AS total_delivered
FROM vw_orders_metrics
WHERE delivery_days IS NOT NULL
GROUP BY customer_state
ORDER BY avg_delivery_days DESC
LIMIT 10
```

### PATTERN 9 — Delivery performance year over year by state
Question: "How has delivery time changed year over year for SP?"
```sql
SELECT order_year,
       ROUND(AVG(delivery_days), 1) AS avg_delivery_days,
       COUNT(*) AS orders
FROM vw_orders_metrics
WHERE delivery_days IS NOT NULL
  AND customer_state = 'SP'
GROUP BY order_year
ORDER BY order_year
LIMIT 5
```

### PATTERN 10 — Cancellation rate overall
Question: "What percentage of orders were cancelled?"
```sql
SELECT ROUND(SUM(CASE WHEN order_status = 'canceled' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS cancel_pct,
       SUM(CASE WHEN order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled_orders,
       COUNT(*) AS total_orders
FROM vw_orders_metrics
LIMIT 1
```

### PATTERN 11 — Category cancellation rate (raw tables required)
Question: "Which product categories have the highest cancellation rates?"
```sql
SELECT t.product_category_name_english AS category,
       COUNT(*) AS total_orders,
       SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled,
       ROUND(SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS cancel_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 100
ORDER BY cancel_pct DESC
LIMIT 10
```

### PATTERN 12 — Freight as % of price by category (raw tables required)
Question: "Which categories have the highest freight cost relative to price?"
```sql
SELECT t.product_category_name_english AS category,
       ROUND(AVG(i.freight_value / NULLIF(i.price, 0)) * 100, 2) AS freight_pct_of_price,
       ROUND(AVG(i.price), 2) AS avg_price,
       ROUND(AVG(i.freight_value), 2) AS avg_freight
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 200
ORDER BY freight_pct_of_price DESC
LIMIT 10
```

### PATTERN 13 — Revenue per unit sold by category
Question: "Which product categories have the highest revenue per unit sold?"
```sql
SELECT category,
       ROUND(total_revenue / NULLIF(total_orders, 0), 2) AS revenue_per_unit,
       total_orders,
       ROUND(total_revenue, 2) AS total_revenue
FROM vw_product_metrics
GROUP BY category, total_revenue, total_orders
ORDER BY revenue_per_unit DESC
LIMIT 10
```

### PATTERN 14 — Multi-condition filter with exclusion
Question: "Top 5 states by revenue excluding São Paulo"
```sql
SELECT customer_state,
       ROUND(SUM(order_revenue), 2) AS total_revenue,
       COUNT(DISTINCT order_id) AS total_orders
FROM vw_orders_metrics
WHERE customer_state != 'SP'
GROUP BY customer_state
ORDER BY total_revenue DESC
LIMIT 5
```

### PATTERN 15 — Average order value comparison across segments
Question: "What is the average order value for delivered vs cancelled orders?"
```sql
SELECT order_status,
       COUNT(*) AS order_count,
       ROUND(AVG(order_revenue), 2) AS avg_order_value,
       ROUND(SUM(order_revenue), 2) AS total_revenue
FROM vw_orders_metrics
WHERE order_status IN ('delivered', 'canceled')
GROUP BY order_status
LIMIT 2
```

### PATTERN 16 — Seller review score distribution
Question: "How many sellers fall into each review score bucket?"
```sql
SELECT CASE
           WHEN avg_review_score >= 4.5 THEN 'Excellent (4.5-5.0)'
           WHEN avg_review_score >= 4.0 THEN 'Good (4.0-4.5)'
           WHEN avg_review_score >= 3.0 THEN 'Average (3.0-4.0)'
           ELSE 'Poor (below 3.0)'
       END AS score_bucket,
       COUNT(*) AS seller_count
FROM vw_seller_metrics
GROUP BY score_bucket
ORDER BY MIN(avg_review_score) DESC
LIMIT 10
```

### PATTERN 17 — Month with highest/lowest metric
Question: "Which month had the highest revenue across all years?"
```sql
SELECT year_month, year, month,
       ROUND(total_revenue, 2) AS revenue
FROM vw_monthly_revenue
ORDER BY total_revenue DESC
LIMIT 1
```

### PATTERN 18 — Growth rate ranking
Question: "Which months had revenue growth above 20%?"
```sql
WITH growth AS (
    SELECT year_month, year, month, total_revenue,
           LAG(total_revenue, 1) OVER (ORDER BY year, month) AS prev_revenue
    FROM vw_monthly_revenue
)
SELECT year_month,
       ROUND(total_revenue, 2) AS revenue,
       ROUND((total_revenue - prev_revenue) / NULLIF(prev_revenue, 0) * 100, 2) AS growth_pct
FROM growth
WHERE prev_revenue IS NOT NULL
  AND (total_revenue - prev_revenue) / NULLIF(prev_revenue, 0) * 100 > 20
ORDER BY growth_pct DESC
LIMIT 12
```

### PATTERN 19 — Seller category performance (raw tables required)
Question: "Which sellers have the most orders in health_beauty?"
```sql
SELECT i.seller_id,
       COUNT(DISTINCT i.order_id) AS orders,
       ROUND(SUM(i.price), 2) AS revenue
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE t.product_category_name_english = 'health_beauty'
GROUP BY i.seller_id
ORDER BY orders DESC
LIMIT 10
```

### PATTERN 20 — Review score trend over time (raw tables required)
Question: "How have average review scores changed over time?"
```sql
SELECT YEAR(r.review_creation_date) AS year,
       MONTH(r.review_creation_date) AS month,
       ROUND(AVG(r.review_score), 2) AS avg_score,
       COUNT(*) AS review_count
FROM olist_order_reviews r
GROUP BY YEAR(r.review_creation_date), MONTH(r.review_creation_date)
HAVING COUNT(*) > 100
ORDER BY year, month
LIMIT 30
```

### PATTERN 21 — States punching above their weight
Question: "Which states have high review scores but lower than average order volume?"
```sql
WITH state_stats AS (
    SELECT customer_state,
           COUNT(DISTINCT order_id) AS orders,
           ROUND(AVG(CASE WHEN delivery_days IS NOT NULL THEN delivery_days END), 1) AS avg_delivery
    FROM vw_orders_metrics
    GROUP BY customer_state
),
avg_orders AS (
    SELECT AVG(orders) AS avg_ord FROM state_stats
),
seller_scores AS (
    SELECT seller_state,
           ROUND(AVG(avg_review_score), 2) AS avg_score
    FROM vw_seller_metrics
    GROUP BY seller_state
)
SELECT s.customer_state,
       s.orders,
       sc.avg_score AS review_score,
       s.avg_delivery AS avg_delivery_days
FROM state_stats s
JOIN seller_scores sc ON s.customer_state = sc.seller_state
CROSS JOIN avg_orders a
WHERE s.orders < a.avg_ord
  AND sc.avg_score >= 4.0
ORDER BY sc.avg_score DESC
LIMIT 10
```

### PATTERN 22 — Seasonal analysis
Question: "Which months consistently have the highest order volume?"
```sql
SELECT month,
       ROUND(AVG(total_orders), 0) AS avg_orders,
       ROUND(AVG(total_revenue), 2) AS avg_revenue,
       COUNT(*) AS years_of_data
FROM vw_monthly_revenue
GROUP BY month
ORDER BY avg_orders DESC
LIMIT 12
```

### PATTERN 23 — Delivery vs estimated (raw tables required)
Question: "What percentage of orders were delivered late vs on time?"
```sql
SELECT
    SUM(CASE WHEN order_delivered_customer_date <= order_estimated_delivery_date THEN 1 ELSE 0 END) AS on_time,
    SUM(CASE WHEN order_delivered_customer_date > order_estimated_delivery_date THEN 1 ELSE 0 END) AS late,
    COUNT(*) AS total_delivered,
    ROUND(SUM(CASE WHEN order_delivered_customer_date > order_estimated_delivery_date THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS late_pct
FROM olist_orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL
  AND order_estimated_delivery_date IS NOT NULL
LIMIT 1
```

### PATTERN 24 — Price band analysis
Question: "How many orders fall into each price range?"
```sql
SELECT CASE
           WHEN order_revenue < 50 THEN 'Under R$50'
           WHEN order_revenue < 100 THEN 'R$50-100'
           WHEN order_revenue < 200 THEN 'R$100-200'
           WHEN order_revenue < 500 THEN 'R$200-500'
           ELSE 'Over R$500'
       END AS price_band,
       COUNT(*) AS order_count,
       ROUND(AVG(order_revenue), 2) AS avg_order_value
FROM vw_orders_metrics
GROUP BY price_band
ORDER BY MIN(order_revenue)
LIMIT 10
```

### PATTERN 25 — Seller concentration (market share)
Question: "What percentage of total revenue comes from the top 10 sellers?"
```sql
WITH total AS (
    SELECT SUM(total_revenue) AS grand_total FROM vw_seller_metrics
),
top10 AS (
    SELECT SUM(total_revenue) AS top10_revenue
    FROM (SELECT total_revenue FROM vw_seller_metrics ORDER BY total_revenue DESC LIMIT 10)
)
SELECT ROUND(top10.top10_revenue, 2) AS top10_revenue,
       ROUND(total.grand_total, 2) AS total_revenue,
       ROUND(top10.top10_revenue * 100.0 / total.grand_total, 2) AS market_share_pct
FROM top10
CROSS JOIN total
LIMIT 1
```

---

## Unanswerable Questions (return message, no SQL)
These cannot be answered from available data:
- Individual customer behaviour over time (no customer history table)
- Seller improvement trends over time (vw_seller_metrics has no time dimension)
- Real-time inventory or stock levels (static dataset)
- Profit margins (no cost data, only revenue)
- Marketing spend or ROI (no marketing data)
- Competitor analysis (single platform dataset)

Pattern for unanswerable:
```sql
SELECT 'This question cannot be answered from the available data.' AS message LIMIT 1
```

### PATTERN 26 — Late delivery % by state (raw tables required)
Question: "What percentage of orders in each state were delivered late?"
```sql
SELECT c.customer_state,
    COUNT(*) AS total_delivered,
    SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) AS late_count,
    ROUND(SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS late_pct
FROM olist_orders o
JOIN olist_customers c ON o.customer_id = c.customer_id
WHERE o.order_status = 'delivered'
  AND o.order_delivered_customer_date IS NOT NULL
  AND o.order_estimated_delivery_date IS NOT NULL
GROUP BY c.customer_state
ORDER BY late_pct DESC
LIMIT 30
```

### PATTERN 27 — Late deliveries by product category (raw tables required)
Question: "Which product categories have the most late deliveries?"
```sql
SELECT t.product_category_name_english AS category,
    COUNT(*) AS total_orders,
    SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) AS late_count,
    ROUND(SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS late_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE o.order_status = 'delivered'
  AND o.order_delivered_customer_date IS NOT NULL
  AND o.order_estimated_delivery_date IS NOT NULL
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 100
ORDER BY late_pct DESC
LIMIT 15
```

### PATTERN 28 — Late delivery rate by seller (raw tables required)
Question: "Which sellers have the highest late delivery rates?"
```sql
SELECT i.seller_id,
    COUNT(*) AS total_delivered,
    SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) AS late_count,
    ROUND(SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS late_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
WHERE o.order_status = 'delivered'
  AND o.order_delivered_customer_date IS NOT NULL
  AND o.order_estimated_delivery_date IS NOT NULL
GROUP BY i.seller_id
HAVING COUNT(*) > 30
ORDER BY late_pct DESC
LIMIT 15
```

### PATTERN 29 — Average days late for late deliveries (raw tables required)
Question: "How much later on average were late deliveries vs estimated date?"
```sql
SELECT
    ROUND(AVG(DATEDIFF(o.order_delivered_customer_date, o.order_estimated_delivery_date)), 1) AS avg_days_late,
    MAX(DATEDIFF(o.order_delivered_customer_date, o.order_estimated_delivery_date)) AS max_days_late,
    COUNT(*) AS late_orders
FROM olist_orders o
WHERE o.order_status = 'delivered'
  AND o.order_delivered_customer_date > o.order_estimated_delivery_date
  AND o.order_delivered_customer_date IS NOT NULL
  AND o.order_estimated_delivery_date IS NOT NULL
LIMIT 1
```

### PATTERN 30 — Late delivery rate by month (raw tables required)
Question: "Which months had the highest late delivery rates?"
```sql
SELECT YEAR(o.order_purchase_timestamp) AS year,
    MONTH(o.order_purchase_timestamp) AS month,
    DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM') AS year_month,
    COUNT(*) AS total_delivered,
    SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) AS late_count,
    ROUND(SUM(CASE WHEN o.order_delivered_customer_date > o.order_estimated_delivery_date THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS late_pct
FROM olist_orders o
WHERE o.order_status = 'delivered'
  AND o.order_delivered_customer_date IS NOT NULL
  AND o.order_estimated_delivery_date IS NOT NULL
GROUP BY YEAR(o.order_purchase_timestamp), MONTH(o.order_purchase_timestamp), DATE_FORMAT(o.order_purchase_timestamp, 'yyyy-MM')
ORDER BY late_pct DESC
LIMIT 12
```

### PATTERN 31 — Payment method distribution (raw tables required)
Question: "What are the most common payment methods used?"
```sql
SELECT payment_type,
    COUNT(*) AS order_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) AS pct_of_orders,
    ROUND(AVG(payment_value), 2) AS avg_payment_value
FROM olist_order_payments
GROUP BY payment_type
ORDER BY order_count DESC
LIMIT 10
```

### PATTERN 32 — Average payment installments per order (raw tables required)
Question: "What is the average number of payment installments per order?"
```sql
SELECT payment_type,
    ROUND(AVG(payment_installments), 1) AS avg_installments,
    MAX(payment_installments) AS max_installments,
    COUNT(*) AS order_count
FROM olist_order_payments
WHERE payment_installments > 0
GROUP BY payment_type
ORDER BY avg_installments DESC
LIMIT 10
```

### PATTERN 33 — Payment method by average order value (raw tables required)
Question: "Which payment methods have the highest average order value?"
```sql
SELECT payment_type,
    ROUND(AVG(payment_value), 2) AS avg_order_value,
    ROUND(SUM(payment_value), 2) AS total_value,
    COUNT(*) AS order_count
FROM olist_order_payments
GROUP BY payment_type
ORDER BY avg_order_value DESC
LIMIT 10
```

### PATTERN 34 — Credit card vs boleto comparison (raw tables required)
Question: "What percentage of orders used credit card vs boleto?"
```sql
SELECT payment_type,
    COUNT(*) AS order_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) AS pct_of_orders,
    ROUND(SUM(payment_value), 2) AS total_value
FROM olist_order_payments
WHERE payment_type IN ('credit_card', 'boleto')
GROUP BY payment_type
ORDER BY order_count DESC
LIMIT 2
```

### PATTERN 35 — Payment preference by state (raw tables required)
Question: "Which states prefer boleto as payment method?"
```sql
SELECT c.customer_state,
    SUM(CASE WHEN p.payment_type = 'boleto' THEN 1 ELSE 0 END) AS boleto_orders,
    COUNT(*) AS total_orders,
    ROUND(SUM(CASE WHEN p.payment_type = 'boleto' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS boleto_pct
FROM olist_order_payments p
JOIN olist_orders o ON p.order_id = o.order_id
JOIN olist_customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state
HAVING COUNT(*) > 100
ORDER BY boleto_pct DESC
LIMIT 15
```

### PATTERN 36 — Order lifecycle funnel (how many orders reach each status)
Question: "How many orders are at each stage of the order lifecycle?"
```sql
SELECT order_status,
    COUNT(*) AS order_count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) AS pct_of_total
FROM olist_orders
GROUP BY order_status
ORDER BY order_count DESC
LIMIT 10
```

### PATTERN 37 — Average time from order to shipment (raw tables required)
Question: "What is the average time between order placement and shipment?"
```sql
SELECT
    ROUND(AVG(DATEDIFF(order_delivered_carrier_date, order_purchase_timestamp)), 1) AS avg_days_to_ship,
    MIN(DATEDIFF(order_delivered_carrier_date, order_purchase_timestamp)) AS min_days,
    MAX(DATEDIFF(order_delivered_carrier_date, order_purchase_timestamp)) AS max_days,
    COUNT(*) AS total_orders
FROM olist_orders
WHERE order_delivered_carrier_date IS NOT NULL
  AND order_purchase_timestamp IS NOT NULL
  AND order_status = 'delivered'
LIMIT 1
```

### PATTERN 38 — Average time from order to approval (raw tables required)
Question: "How long does it take on average from order placement to approval?"
```sql
SELECT
    ROUND(AVG(DATEDIFF(order_approved_at, order_purchase_timestamp)), 1) AS avg_days_to_approve,
    ROUND(AVG(DATEDIFF(order_approved_at, order_purchase_timestamp) * 24), 1) AS avg_hours_to_approve,
    COUNT(*) AS total_orders
FROM olist_orders
WHERE order_approved_at IS NOT NULL
  AND order_purchase_timestamp IS NOT NULL
LIMIT 1
```

### PATTERN 39 — Orders by day of week (raw tables required)
Question: "Which day of the week has the most orders placed?"
```sql
SELECT DAYOFWEEK(order_purchase_timestamp) AS day_of_week_num,
    CASE DAYOFWEEK(order_purchase_timestamp)
        WHEN 1 THEN 'Sunday'
        WHEN 2 THEN 'Monday'
        WHEN 3 THEN 'Tuesday'
        WHEN 4 THEN 'Wednesday'
        WHEN 5 THEN 'Thursday'
        WHEN 6 THEN 'Friday'
        WHEN 7 THEN 'Saturday'
    END AS day_name,
    COUNT(*) AS order_count,
    ROUND(AVG(i.price), 2) AS avg_order_value
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
GROUP BY DAYOFWEEK(order_purchase_timestamp)
ORDER BY order_count DESC
LIMIT 7
```

### PATTERN 40 — Orders by hour of day (raw tables required)
Question: "Which hour of the day has the most orders placed?"
```sql
SELECT HOUR(order_purchase_timestamp) AS hour_of_day,
    COUNT(*) AS order_count
FROM olist_orders
GROUP BY HOUR(order_purchase_timestamp)
ORDER BY order_count DESC
LIMIT 24
```

### PATTERN 41 — Weekend vs weekday orders (raw tables required)
Question: "How many orders were placed on weekends vs weekdays?"
```sql
SELECT
    CASE WHEN DAYOFWEEK(order_purchase_timestamp) IN (1, 7) THEN 'Weekend' ELSE 'Weekday' END AS day_type,
    COUNT(*) AS order_count,
    ROUND(AVG(i.price), 2) AS avg_order_value,
    ROUND(SUM(i.price), 2) AS total_revenue
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
GROUP BY day_type
ORDER BY order_count DESC
LIMIT 2
```

### PATTERN 42 — Repeat product purchases (how many products ordered more than once)
Question: "Which products have been ordered the most times?"
```sql
SELECT p.product_id,
    COALESCE(t.product_category_name_english, p.product_category_name, 'unknown') AS category,
    COUNT(DISTINCT i.order_id) AS total_orders,
    ROUND(SUM(i.price), 2) AS total_revenue,
    ROUND(AVG(i.price), 2) AS avg_price
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
LEFT JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY p.product_id, category
ORDER BY total_orders DESC
LIMIT 15
```

### PATTERN 43 — Category revenue share (% of total revenue per category)
Question: "What is each product category's share of total revenue?"
```sql
WITH category_rev AS (
    SELECT t.product_category_name_english AS category,
        ROUND(SUM(i.price), 2) AS category_revenue
    FROM olist_order_items i
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    GROUP BY t.product_category_name_english
),
total AS (SELECT SUM(category_revenue) AS grand_total FROM category_rev)
SELECT cr.category,
    cr.category_revenue,
    ROUND(cr.category_revenue * 100.0 / t.grand_total, 2) AS revenue_share_pct
FROM category_rev cr
CROSS JOIN total t
ORDER BY cr.category_revenue DESC
LIMIT 20
```

### PATTERN 44 — Category order count share (% of total orders per category)
Question: "What percentage of all orders does each category represent?"
```sql
WITH cat_orders AS (
    SELECT t.product_category_name_english AS category,
        COUNT(DISTINCT i.order_id) AS cat_order_count
    FROM olist_order_items i
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    GROUP BY t.product_category_name_english
),
total AS (SELECT SUM(cat_order_count) AS grand_total FROM cat_orders)
SELECT co.category,
    co.cat_order_count,
    ROUND(co.cat_order_count * 100.0 / t.grand_total, 2) AS order_share_pct
FROM cat_orders co
CROSS JOIN total t
ORDER BY co.cat_order_count DESC
LIMIT 20
```

### PATTERN 45 — Seller geographic distribution
Question: "How many sellers are in each state?"
```sql
SELECT seller_state,
    COUNT(DISTINCT seller_id) AS seller_count,
    ROUND(COUNT(DISTINCT seller_id) * 100.0 / SUM(COUNT(DISTINCT seller_id)) OVER (), 2) AS pct_of_sellers
FROM olist_sellers
GROUP BY seller_state
ORDER BY seller_count DESC
LIMIT 27
```

### PATTERN 46 — Customer geographic distribution
Question: "How many customers are in each state?"
```sql
SELECT customer_state,
    COUNT(DISTINCT customer_id) AS customer_count,
    ROUND(COUNT(DISTINCT customer_id) * 100.0 / SUM(COUNT(DISTINCT customer_id)) OVER (), 2) AS pct_of_customers
FROM olist_customers
GROUP BY customer_state
ORDER BY customer_count DESC
LIMIT 27
```

### PATTERN 47 — Revenue by customer state (use view)
Question: "Which states generate the most revenue?"
```sql
SELECT customer_state,
    ROUND(SUM(order_revenue), 2) AS total_revenue,
    COUNT(DISTINCT order_id) AS total_orders,
    ROUND(AVG(order_revenue), 2) AS avg_order_value
FROM vw_orders_metrics
WHERE order_status = 'delivered'
GROUP BY customer_state
ORDER BY total_revenue DESC
LIMIT 15
```

### PATTERN 48 — Seller vs customer state mismatch (cross-state orders, raw tables)
Question: "How many orders are cross-state — seller and customer in different states?"
```sql
SELECT
    SUM(CASE WHEN s.seller_state != c.customer_state THEN 1 ELSE 0 END) AS cross_state_orders,
    SUM(CASE WHEN s.seller_state = c.customer_state THEN 1 ELSE 0 END) AS same_state_orders,
    COUNT(*) AS total_orders,
    ROUND(SUM(CASE WHEN s.seller_state != c.customer_state THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS cross_state_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_sellers s ON i.seller_id = s.seller_id
JOIN olist_customers c ON o.customer_id = c.customer_id
WHERE o.order_status = 'delivered'
LIMIT 1
```

### PATTERN 49 — Review score by state (raw tables required)
Question: "What is the average review score per state?"
```sql
SELECT c.customer_state,
    ROUND(AVG(r.review_score), 2) AS avg_review_score,
    COUNT(r.review_id) AS review_count
FROM olist_order_reviews r
JOIN olist_orders o ON r.order_id = o.order_id
JOIN olist_customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state
HAVING COUNT(r.review_id) > 50
ORDER BY avg_review_score DESC
LIMIT 27
```

### PATTERN 50 — 1-star vs 5-star review counts by category (raw tables required)
Question: "Which categories have the most 1-star reviews? Which have the most 5-star?"
```sql
SELECT t.product_category_name_english AS category,
    SUM(CASE WHEN r.review_score = 1 THEN 1 ELSE 0 END) AS one_star,
    SUM(CASE WHEN r.review_score = 5 THEN 1 ELSE 0 END) AS five_star,
    COUNT(*) AS total_reviews,
    ROUND(AVG(r.review_score), 2) AS avg_score,
    ROUND(SUM(CASE WHEN r.review_score = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS one_star_pct
FROM olist_order_reviews r
JOIN olist_orders o ON r.order_id = o.order_id
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 100
ORDER BY one_star DESC
LIMIT 15
```

### PATTERN 51 — Review score improving or declining categories YoY (raw tables required)
Question: "Which product categories have improving review scores from 2017 to 2018?"
```sql
WITH yearly AS (
    SELECT t.product_category_name_english AS category,
        YEAR(r.review_creation_date) AS yr,
        ROUND(AVG(r.review_score), 3) AS avg_score,
        COUNT(*) AS review_count
    FROM olist_order_reviews r
    JOIN olist_orders o ON r.order_id = o.order_id
    JOIN olist_order_items i ON o.order_id = i.order_id
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE YEAR(r.review_creation_date) IN (2017, 2018)
    GROUP BY t.product_category_name_english, YEAR(r.review_creation_date)
    HAVING COUNT(*) > 30
)
SELECT y17.category,
    y17.avg_score AS score_2017,
    y18.avg_score AS score_2018,
    ROUND(y18.avg_score - y17.avg_score, 3) AS score_change
FROM yearly y17
JOIN yearly y18 ON y17.category = y18.category AND y17.yr = 2017 AND y18.yr = 2018
ORDER BY score_change DESC
LIMIT 15
```

### PATTERN 52 — Seller improving review scores YoY (raw tables required)
Question: "Which sellers improved their review scores from 2017 to 2018?"
```sql
WITH yearly AS (
    SELECT i.seller_id,
        YEAR(r.review_creation_date) AS yr,
        ROUND(AVG(r.review_score), 3) AS avg_score,
        COUNT(*) AS review_count
    FROM olist_order_reviews r
    JOIN olist_orders o ON r.order_id = o.order_id
    JOIN olist_order_items i ON o.order_id = i.order_id
    WHERE YEAR(r.review_creation_date) IN (2017, 2018)
    GROUP BY i.seller_id, YEAR(r.review_creation_date)
    HAVING COUNT(*) > 20
)
SELECT y17.seller_id,
    y17.avg_score AS score_2017,
    y18.avg_score AS score_2018,
    ROUND(y18.avg_score - y17.avg_score, 3) AS improvement
FROM yearly y17
JOIN yearly y18 ON y17.seller_id = y18.seller_id AND y17.yr = 2017 AND y18.yr = 2018
WHERE y18.avg_score > y17.avg_score
ORDER BY improvement DESC
LIMIT 15
```

### PATTERN 53 — Category growth YoY (order volume 2017 vs 2018, raw tables)
Question: "Which categories grew the most in order volume from 2017 to 2018?"
```sql
WITH yearly AS (
    SELECT t.product_category_name_english AS category,
        YEAR(o.order_purchase_timestamp) AS yr,
        COUNT(DISTINCT o.order_id) AS order_count
    FROM olist_orders o
    JOIN olist_order_items i ON o.order_id = i.order_id
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE YEAR(o.order_purchase_timestamp) IN (2017, 2018)
    GROUP BY t.product_category_name_english, YEAR(o.order_purchase_timestamp)
    HAVING COUNT(DISTINCT o.order_id) > 50
)
SELECT y17.category,
    y17.order_count AS orders_2017,
    y18.order_count AS orders_2018,
    ROUND((y18.order_count - y17.order_count) * 100.0 / y17.order_count, 1) AS growth_pct
FROM yearly y17
JOIN yearly y18 ON y17.category = y18.category AND y17.yr = 2017 AND y18.yr = 2018
ORDER BY growth_pct DESC
LIMIT 15
```

### PATTERN 54 — Revenue per product weight (value density by category, raw tables)
Question: "Which categories have the highest revenue per gram of product weight?"
```sql
SELECT t.product_category_name_english AS category,
    ROUND(SUM(i.price) / NULLIF(SUM(p.product_weight_g), 0), 4) AS revenue_per_gram,
    ROUND(AVG(p.product_weight_g), 0) AS avg_weight_g,
    ROUND(SUM(i.price), 2) AS total_revenue,
    COUNT(*) AS order_count
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE p.product_weight_g > 0
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 100
ORDER BY revenue_per_gram DESC
LIMIT 15
```

### PATTERN 55 — Product size analysis (volume by category, raw tables)
Question: "Which categories have the largest average product dimensions?"
```sql
SELECT t.product_category_name_english AS category,
    ROUND(AVG(p.product_weight_g), 0) AS avg_weight_g,
    ROUND(AVG(p.product_length_cm * p.product_height_cm * p.product_width_cm), 0) AS avg_volume_cm3,
    ROUND(AVG(p.product_length_cm), 1) AS avg_length_cm,
    COUNT(*) AS product_count
FROM olist_products p
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE p.product_weight_g > 0
  AND p.product_length_cm > 0
GROUP BY t.product_category_name_english
ORDER BY avg_volume_cm3 DESC
LIMIT 15
```

### PATTERN 56 — Seller market share within a category (raw tables)
Question: "What is the market share of top sellers within a specific category?"
```sql
WITH cat_total AS (
    SELECT SUM(i.price) AS total_revenue
    FROM olist_order_items i
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    WHERE t.product_category_name_english = 'health_beauty'
)
SELECT i.seller_id,
    ROUND(SUM(i.price), 2) AS seller_revenue,
    ROUND(SUM(i.price) * 100.0 / ct.total_revenue, 2) AS market_share_pct,
    COUNT(DISTINCT i.order_id) AS order_count
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
CROSS JOIN cat_total ct
WHERE t.product_category_name_english = 'health_beauty'
GROUP BY i.seller_id, ct.total_revenue
ORDER BY seller_revenue DESC
LIMIT 10
```

### PATTERN 57 — Orders with multiple items vs single item (raw tables)
Question: "What percentage of orders contain more than one item?"
```sql
WITH order_sizes AS (
    SELECT order_id, COUNT(*) AS item_count
    FROM olist_order_items
    GROUP BY order_id
)
SELECT
    SUM(CASE WHEN item_count = 1 THEN 1 ELSE 0 END) AS single_item_orders,
    SUM(CASE WHEN item_count > 1 THEN 1 ELSE 0 END) AS multi_item_orders,
    COUNT(*) AS total_orders,
    ROUND(SUM(CASE WHEN item_count > 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS multi_item_pct,
    ROUND(AVG(item_count), 2) AS avg_items_per_order
FROM order_sizes
LIMIT 1
```

### PATTERN 58 — High value orders analysis (raw tables)
Question: "What are the characteristics of orders over R$500?"
```sql
SELECT
    COUNT(*) AS high_value_orders,
    ROUND(AVG(order_revenue), 2) AS avg_revenue,
    ROUND(AVG(delivery_days), 1) AS avg_delivery_days,
    ROUND(AVG(CASE WHEN order_status = 'canceled' THEN 1.0 ELSE 0.0 END) * 100, 2) AS cancel_rate_pct,
    customer_state
FROM vw_orders_metrics
WHERE order_revenue > 500
GROUP BY customer_state
ORDER BY high_value_orders DESC
LIMIT 10
```

### PATTERN 59 — Correlation between delivery speed and review score (view)
Question: "Do faster deliveries get better review scores?"
```sql
SELECT
    CASE
        WHEN delivery_days <= 7 THEN 'Fast (≤7 days)'
        WHEN delivery_days <= 14 THEN 'Normal (8-14 days)'
        WHEN delivery_days <= 21 THEN 'Slow (15-21 days)'
        ELSE 'Very Slow (>21 days)'
    END AS delivery_bucket,
    COUNT(*) AS order_count,
    ROUND(AVG(delivery_days), 1) AS avg_days
FROM vw_orders_metrics
WHERE delivery_days IS NOT NULL
  AND order_status = 'delivered'
GROUP BY delivery_bucket
ORDER BY MIN(delivery_days)
LIMIT 10
```

### PATTERN 60 — Freight cost impact on order value (view)
Question: "What percentage of total order value is freight on average?"
```sql
SELECT
    ROUND(AVG(order_freight / NULLIF(order_total, 0)) * 100, 2) AS avg_freight_pct_of_total,
    ROUND(AVG(order_freight), 2) AS avg_freight_value,
    ROUND(AVG(order_revenue), 2) AS avg_item_value,
    ROUND(AVG(order_total), 2) AS avg_total_value,
    COUNT(*) AS total_orders
FROM vw_orders_metrics
WHERE order_status = 'delivered'
  AND order_total > 0
LIMIT 1
```

### PATTERN 61 — Top categories by average review score (view)
Question: "Which product categories have the highest average review scores?"
```sql
SELECT category,
    ROUND(avg_review_score, 2) AS avg_score,
    SUM(total_orders) AS total_orders,
    ROUND(SUM(total_revenue), 2) AS total_revenue
FROM vw_product_metrics
WHERE avg_review_score IS NOT NULL
GROUP BY category, avg_review_score
HAVING SUM(total_orders) > 100
ORDER BY avg_review_score DESC
LIMIT 15
```

### PATTERN 62 — Categories with high volume but low review (underperforming, view)
Question: "Which high-volume categories have below average review scores?"
```sql
WITH avg_score AS (
    SELECT AVG(avg_review_score) AS platform_avg FROM vw_product_metrics
)
SELECT p.category,
    SUM(p.total_orders) AS total_orders,
    ROUND(AVG(p.avg_review_score), 2) AS avg_score,
    ROUND(a.platform_avg, 2) AS platform_avg
FROM vw_product_metrics p
CROSS JOIN avg_score a
GROUP BY p.category, a.platform_avg
HAVING SUM(p.total_orders) > 500
   AND AVG(p.avg_review_score) < a.platform_avg
ORDER BY total_orders DESC
LIMIT 15
```

### PATTERN 63 — Monthly cancellation trend (view)
Question: "How has the monthly cancellation rate trended over time?"
```sql
SELECT year_month, year, month,
    total_orders,
    canceled_orders,
    ROUND(canceled_orders * 100.0 / NULLIF(total_orders, 0), 2) AS cancel_pct,
    LAG(ROUND(canceled_orders * 100.0 / NULLIF(total_orders, 0), 2)) OVER (ORDER BY year, month) AS prev_cancel_pct
FROM vw_monthly_revenue
WHERE total_orders > 0
ORDER BY year, month
LIMIT 30
```

### PATTERN 64 — Quarterly revenue summary (view)
Question: "What was revenue by quarter?"
```sql
SELECT year,
    CEIL(month / 3.0) AS quarter,
    CONCAT(year, '-Q', CAST(CEIL(month / 3.0) AS STRING)) AS quarter_label,
    SUM(total_revenue) AS quarterly_revenue,
    SUM(total_orders) AS quarterly_orders,
    ROUND(AVG(avg_order_value), 2) AS avg_order_value
FROM vw_monthly_revenue
GROUP BY year, CEIL(month / 3.0)
ORDER BY year, quarter
LIMIT 12
```

### PATTERN 65 — Revenue acceleration (months where growth itself grew, view)
Question: "Which months had revenue growth above the average growth rate?"
```sql
WITH monthly AS (
    SELECT year_month, year, month, total_revenue,
        LAG(total_revenue) OVER (ORDER BY year, month) AS prev_revenue
    FROM vw_monthly_revenue
),
growth AS (
    SELECT year_month, year, month, total_revenue, prev_revenue,
        ROUND((total_revenue - prev_revenue) / NULLIF(prev_revenue, 0) * 100, 2) AS growth_pct
    FROM monthly
    WHERE prev_revenue IS NOT NULL AND prev_revenue > 0
),
avg_growth AS (
    SELECT AVG(growth_pct) AS avg_pct FROM growth
)
SELECT g.year_month, g.total_revenue, g.growth_pct, a.avg_pct AS avg_growth_pct
FROM growth g
CROSS JOIN avg_growth a
WHERE g.growth_pct > a.avg_pct
ORDER BY g.growth_pct DESC
LIMIT 12
```

### PATTERN 66 — Seller count by revenue tier (view)
Question: "How many sellers fall into each revenue tier?"
```sql
SELECT
    CASE
        WHEN total_revenue >= 100000 THEN 'Tier 1 — R$100k+'
        WHEN total_revenue >= 50000  THEN 'Tier 2 — R$50k-100k'
        WHEN total_revenue >= 10000  THEN 'Tier 3 — R$10k-50k'
        WHEN total_revenue >= 1000   THEN 'Tier 4 — R$1k-10k'
        ELSE 'Tier 5 — Under R$1k'
    END AS revenue_tier,
    COUNT(*) AS seller_count,
    ROUND(SUM(total_revenue), 2) AS tier_revenue
FROM vw_seller_metrics
GROUP BY revenue_tier
ORDER BY MIN(total_revenue) DESC
LIMIT 10
```

### PATTERN 67 — Sellers with high orders but low revenue (low AOV sellers, view)
Question: "Which sellers have many orders but low average order value?"
```sql
WITH avg_aov AS (SELECT AVG(avg_order_value) AS platform_avg FROM vw_seller_metrics)
SELECT s.seller_id, s.seller_state,
    s.total_orders,
    ROUND(s.avg_order_value, 2) AS avg_order_value,
    ROUND(s.total_revenue, 2) AS total_revenue,
    ROUND(a.platform_avg, 2) AS platform_avg_aov
FROM vw_seller_metrics s
CROSS JOIN avg_aov a
WHERE s.total_orders > 50
  AND s.avg_order_value < a.platform_avg * 0.5
ORDER BY s.total_orders DESC
LIMIT 15
```

### PATTERN 68 — Top N sellers responsible for X% of revenue (Pareto, view)
Question: "How many sellers are responsible for 80% of total revenue?"
```sql
WITH ranked AS (
    SELECT seller_id, total_revenue,
        SUM(total_revenue) OVER (ORDER BY total_revenue DESC) AS cumulative_revenue,
        SUM(total_revenue) OVER () AS grand_total
    FROM vw_seller_metrics
)
SELECT COUNT(*) AS sellers_for_80pct,
    ROUND(MAX(cumulative_revenue), 2) AS cumulative_rev,
    ROUND(MAX(grand_total), 2) AS total_rev
FROM ranked
WHERE cumulative_revenue <= grand_total * 0.8
LIMIT 1
```

### PATTERN 69 — New customers per month (unique customers first order, raw tables)
Question: "How many new customers placed their first order each month?"
```sql
WITH first_orders AS (
    SELECT customer_id,
        MIN(order_purchase_timestamp) AS first_order_date
    FROM olist_orders
    WHERE order_status != 'canceled'
    GROUP BY customer_id
)
SELECT DATE_FORMAT(first_order_date, 'yyyy-MM') AS year_month,
    COUNT(*) AS new_customers
FROM first_orders
GROUP BY DATE_FORMAT(first_order_date, 'yyyy-MM')
ORDER BY year_month
LIMIT 30
```

### PATTERN 70 — Average review score by payment method (raw tables)
Question: "Do customers who pay with credit card give higher review scores than boleto?"
```sql
SELECT p.payment_type,
    ROUND(AVG(r.review_score), 2) AS avg_review_score,
    COUNT(*) AS review_count,
    ROUND(AVG(p.payment_value), 2) AS avg_payment_value
FROM olist_order_payments p
JOIN olist_order_reviews r ON p.order_id = r.order_id
GROUP BY p.payment_type
ORDER BY avg_review_score DESC
LIMIT 10
```

### PATTERN 71 — High installment orders analysis (raw tables)
Question: "What is the average review score for orders with more than 6 installments?"
```sql
SELECT
    CASE WHEN p.payment_installments > 6 THEN 'High (>6)' ELSE 'Low (1-6)' END AS installment_group,
    ROUND(AVG(r.review_score), 2) AS avg_review_score,
    ROUND(AVG(p.payment_value), 2) AS avg_order_value,
    COUNT(*) AS order_count
FROM olist_order_payments p
JOIN olist_order_reviews r ON p.order_id = r.order_id
WHERE p.payment_type = 'credit_card'
GROUP BY installment_group
ORDER BY installment_group
LIMIT 2
```

### PATTERN 72 — Delivery time by product weight bucket (raw tables)
Question: "Do heavier products take longer to deliver?"
```sql
WITH order_weights AS (
    SELECT o.order_id,
        DATEDIFF(o.order_delivered_customer_date, o.order_purchase_timestamp) AS delivery_days,
        AVG(p.product_weight_g) AS avg_weight
    FROM olist_orders o
    JOIN olist_order_items i ON o.order_id = i.order_id
    JOIN olist_products p ON i.product_id = p.product_id
    WHERE o.order_status = 'delivered'
      AND o.order_delivered_customer_date IS NOT NULL
      AND p.product_weight_g > 0
    GROUP BY o.order_id, o.order_delivered_customer_date, o.order_purchase_timestamp
)
SELECT
    CASE
        WHEN avg_weight < 500   THEN 'Light (<500g)'
        WHEN avg_weight < 2000  THEN 'Medium (500g-2kg)'
        WHEN avg_weight < 5000  THEN 'Heavy (2kg-5kg)'
        ELSE 'Very Heavy (>5kg)'
    END AS weight_bucket,
    ROUND(AVG(delivery_days), 1) AS avg_delivery_days,
    COUNT(*) AS order_count
FROM order_weights
GROUP BY weight_bucket
ORDER BY MIN(avg_weight)
LIMIT 4
```

### PATTERN 73 — States with both high revenue and fast delivery (multi-metric, view)
Question: "Which states have above average revenue AND above average delivery speed?"
```sql
WITH state_stats AS (
    SELECT customer_state,
        ROUND(SUM(order_revenue), 2) AS total_revenue,
        ROUND(AVG(delivery_days), 1) AS avg_delivery_days,
        COUNT(DISTINCT order_id) AS total_orders
    FROM vw_orders_metrics
    WHERE delivery_days IS NOT NULL AND order_status = 'delivered'
    GROUP BY customer_state
),
avgs AS (
    SELECT AVG(total_revenue) AS avg_rev, AVG(avg_delivery_days) AS avg_days
    FROM state_stats
)
SELECT s.customer_state, s.total_revenue, s.avg_delivery_days, s.total_orders
FROM state_stats s
CROSS JOIN avgs a
WHERE s.total_revenue > a.avg_rev
  AND s.avg_delivery_days < a.avg_days
ORDER BY s.total_revenue DESC
LIMIT 10
```

### PATTERN 74 — Product categories ordered with multiple sellers (raw tables)
Question: "Which categories have the most unique sellers?"
```sql
SELECT t.product_category_name_english AS category,
    COUNT(DISTINCT i.seller_id) AS unique_sellers,
    COUNT(DISTINCT i.order_id) AS total_orders,
    ROUND(SUM(i.price), 2) AS total_revenue
FROM olist_order_items i
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
GROUP BY t.product_category_name_english
ORDER BY unique_sellers DESC
LIMIT 15
```

### PATTERN 75 — Orders cancelled after approval (raw tables)
Question: "How many orders were cancelled after being approved?"
```sql
SELECT
    SUM(CASE WHEN order_approved_at IS NOT NULL AND order_status = 'canceled' THEN 1 ELSE 0 END) AS cancelled_after_approval,
    SUM(CASE WHEN order_approved_at IS NULL AND order_status = 'canceled' THEN 1 ELSE 0 END) AS cancelled_before_approval,
    COUNT(CASE WHEN order_status = 'canceled' THEN 1 END) AS total_cancelled
FROM olist_orders
LIMIT 1
```

### PATTERN 76 — Revenue concentration by top categories (Pareto, raw tables)
Question: "What percentage of total revenue comes from the top 5 categories?"
```sql
WITH cat_rev AS (
    SELECT t.product_category_name_english AS category,
        ROUND(SUM(i.price), 2) AS revenue
    FROM olist_order_items i
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    GROUP BY t.product_category_name_english
    ORDER BY revenue DESC
    LIMIT 5
),
total AS (
    SELECT SUM(i.price) AS grand_total FROM olist_order_items i
)
SELECT SUM(cr.revenue) AS top5_revenue,
    t.grand_total,
    ROUND(SUM(cr.revenue) * 100.0 / t.grand_total, 2) AS top5_share_pct
FROM cat_rev cr
CROSS JOIN total t
GROUP BY t.grand_total
LIMIT 1
```

### PATTERN 77 — Average photos per product by category (raw tables)
Question: "Which categories have products with the most photos?"
```sql
SELECT t.product_category_name_english AS category,
    ROUND(AVG(p.product_photos_qty), 1) AS avg_photos,
    COUNT(*) AS product_count
FROM olist_products p
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE p.product_photos_qty IS NOT NULL
GROUP BY t.product_category_name_english
ORDER BY avg_photos DESC
LIMIT 15
```

### PATTERN 78 — Orders with no review (raw tables)
Question: "What percentage of delivered orders did not receive a review?"
```sql
SELECT
    COUNT(DISTINCT o.order_id) AS total_delivered,
    COUNT(DISTINCT r.order_id) AS reviewed_orders,
    COUNT(DISTINCT o.order_id) - COUNT(DISTINCT r.order_id) AS no_review_orders,
    ROUND((COUNT(DISTINCT o.order_id) - COUNT(DISTINCT r.order_id)) * 100.0 / COUNT(DISTINCT o.order_id), 2) AS no_review_pct
FROM olist_orders o
LEFT JOIN olist_order_reviews r ON o.order_id = r.order_id
WHERE o.order_status = 'delivered'
LIMIT 1
```

### PATTERN 79 — Sellers selling in only one category vs multiple (raw tables)
Question: "How many sellers sell in only one category vs multiple categories?"
```sql
WITH seller_cats AS (
    SELECT i.seller_id,
        COUNT(DISTINCT t.product_category_name_english) AS category_count
    FROM olist_order_items i
    JOIN olist_products p ON i.product_id = p.product_id
    JOIN product_category_translation t ON p.product_category_name = t.product_category_name
    GROUP BY i.seller_id
)
SELECT
    SUM(CASE WHEN category_count = 1 THEN 1 ELSE 0 END) AS single_category_sellers,
    SUM(CASE WHEN category_count > 1 THEN 1 ELSE 0 END) AS multi_category_sellers,
    COUNT(*) AS total_sellers,
    ROUND(AVG(category_count), 1) AS avg_categories_per_seller
FROM seller_cats
LIMIT 1
```

### PATTERN 80 — Unanswerable questions — return message, no SQL
These questions cannot be answered from available data. Return the message pattern:
- Customer lifetime value or repeat purchase rate (no customer history across orders)
- Profit margins (no cost data, only revenue)
- Inventory or stock levels (static dataset)
- Marketing spend or ROI (no marketing data)
- Real-time or future data
- Competitor analysis (single platform)
- Seller improvement trends over time (vw_seller_metrics has no time dimension — lifetime aggregates only)
```sql
SELECT 'This question cannot be answered from the available data.' AS message LIMIT 1
```


### PATTERN 81 — Learned from user query
Question: "In 2018 august which product category had the highest cancellation rate"
```sql
SELECT t.product_category_name_english AS category,
       COUNT(*) AS total_orders,
       SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled_orders,
       ROUND(SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS cancel_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE YEAR(o.order_purchase_timestamp) = 2018
  AND MONTH(o.order_purchase_timestamp) = 8
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 0
ORDER BY cancel_pct DESC
LIMIT 1
```


### PATTERN 82 — Learned from user query
Question: "which product category has high cancel rate in 2016 february"
```sql
SELECT t.product_category_name_english AS category, COUNT(*) AS total_orders, SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled_orders, ROUND(SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS cancel_pct FROM olist_orders o JOIN olist_order_items i ON o.order_id = i.order_id JOIN olist_products p ON i.product_id = p.product_id JOIN product_category_translation t ON p.product_category_name = t.product_category_name WHERE YEAR(o.order_purchase_timestamp) = 2016 AND MONTH(o.order_purchase_timestamp) = 2 GROUP BY t.product_category_name_english HAVING COUNT(*) > 100 ORDER BY cancel_pct DESC LIMIT 1
```


### PATTERN 83 — Learned from user query
Question: "which product category had highest cancel rate in 2018 august"
```sql
SELECT t.product_category_name_english AS category,
       COUNT(*) AS total_orders,
       SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled_orders,
       ROUND(SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS cancel_pct
FROM olist_orders o
JOIN olist_order_items i ON o.order_id = i.order_id
JOIN olist_products p ON i.product_id = p.product_id
JOIN product_category_translation t ON p.product_category_name = t.product_category_name
WHERE YEAR(o.order_purchase_timestamp) = 2018
  AND MONTH(o.order_purchase_timestamp) = 8
GROUP BY t.product_category_name_english
HAVING COUNT(*) > 0
ORDER BY cancel_pct DESC
LIMIT 1
```


### PATTERN 84 — Learned from user query
Question: "In 2018, how many orders were cancelled in an year and in which month we saw highest cancellation percentage"
```sql
SELECT SUM(canceled_orders) AS total_canceled_orders,
    month,
    MAX(cancel_pct) AS highest_cancellation_percentage
FROM (
    SELECT 
        month,
        canceled_orders,
        total_orders,
        ROUND(canceled_orders * 100.0 / NULLIF(total_orders, 0), 2) AS cancel_pct
    FROM vw_monthly_revenue
    WHERE year = 2018
)
GROUP BY month, canceled_orders, total_orders
ORDER BY highest_cancellation_percentage DESC
LIMIT 1
```


### PATTERN 85 — Learned from user query
Question: "In 2018, I want total orders fulfilled, cancelled and reviewed in an year"
```sql
SELECT COUNT(*) AS total_orders,
    SUM(CASE WHEN order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled_orders,
    SUM(CASE WHEN order_status != 'canceled' THEN 1 ELSE 0 END) AS fulfilled_orders,
    SUM(CASE WHEN review_id IS NOT NULL THEN 1 ELSE 0 END) AS reviewed_orders
FROM olist_orders o
LEFT JOIN olist_order_reviews r ON o.order_id = r.order_id
WHERE YEAR(order_purchase_timestamp) = 2018
LIMIT 1
```
