# Olist Business Logic

> IMPORTANT: This document contains business context ONLY.
> "business_logic" is NOT a table or view in Databricks.
> NEVER use business_logic in any SQL query.
> Only use: vw_orders_metrics, vw_seller_metrics, vw_product_metrics, vw_monthly_revenue
> OR raw tables: olist_orders, olist_order_items, olist_products, product_category_translation, olist_order_reviews, olist_sellers, olist_customers, olist_order_payments

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

## Corporate & MBA Abbreviations → Data Mapping

### Temporal
| Abbreviation | Meaning | How to query |
|---|---|---|
| YoY | Year over Year | Compare same metric across years using `vw_monthly_revenue GROUP BY year` |
| MoM | Month over Month | Use `LAG(total_revenue,1) OVER (ORDER BY year, month)` on `vw_monthly_revenue` |
| QoQ | Quarter over Quarter | Group by `CEIL(month/3.0)` on `vw_monthly_revenue` |
| WoW | Week over Week | Not available — dataset has no week-level granularity |
| YTD | Year to Date | Filter `WHERE order_year = <year> AND order_month <= <current_month>` |
| MTD | Month to Date | Filter `WHERE order_year = <year> AND order_month = <month>` |
| QTD | Quarter to Date | Filter by year and quarter range on `vw_monthly_revenue` |
| H1 | First Half (Jan–Jun) | `WHERE order_month BETWEEN 1 AND 6` |
| H2 | Second Half (Jul–Dec) | `WHERE order_month BETWEEN 7 AND 12` |
| FY | Fiscal Year | Treat as calendar year — filter by `order_year` |
| LTM / TTM | Last / Trailing Twelve Months | Last 12 months of data from `vw_monthly_revenue` |
| Q1 / Q2 / Q3 / Q4 | Quarter 1–4 | `CEIL(month/3.0) = 1/2/3/4` on `vw_monthly_revenue` |

### Revenue & Finance
| Abbreviation | Meaning | How to query |
|---|---|---|
| AOV / ATV | Average Order Value | `AVG(order_revenue)` from `vw_orders_metrics` or `avg_order_value` from `vw_seller_metrics` |
| GMV | Gross Merchandise Value | `SUM(order_total)` from `vw_orders_metrics` (revenue + freight) |
| ASP | Average Selling Price | `AVG(avg_price)` from `vw_product_metrics` |
| ARR | Annual Recurring Revenue | `SUM(total_revenue)` from `vw_monthly_revenue WHERE year = <year>` |
| MRR | Monthly Recurring Revenue | `total_revenue` from `vw_monthly_revenue` for a given month |
| LTV / CLV | Customer Lifetime Value | Not directly available — no repeat-purchase history |
| CAC | Customer Acquisition Cost | Not available — no marketing spend data |
| ROAS / ROI | Return on Ad/Investment Spend | Not available — no cost data |
| GP / Gross Margin | Gross Profit / Margin | Not available — no cost data, only revenue |
| Rev | Revenue | `order_revenue` from `vw_orders_metrics` or `total_revenue` from `vw_monthly_revenue` |

### Delivery & Operations
| Abbreviation | Meaning | How to query |
|---|---|---|
| OTD | On Time Delivery | Orders where `order_delivered_customer_date <= order_estimated_delivery_date` in `olist_orders` |
| OTIF | On Time In Full | Treat as OTD for this dataset (no partial fulfilment data) |
| TAT | Turnaround Time | `DATEDIFF(order_delivered_carrier_date, order_purchase_timestamp)` in `olist_orders` |
| ETA / ETD / EDD | Estimated Delivery Date | `order_estimated_delivery_date` in `olist_orders` |
| SLA | Service Level Agreement | Use delivery threshold: >20 days = SLA breach |

### Customer & Satisfaction
| Abbreviation | Meaning | How to query |
|---|---|---|
| NPS / CSAT | Net Promoter / Customer Satisfaction Score | Proxy: `review_score` from `olist_order_reviews` (scale 1–5) |
| Churn | Lost customers | Customers with no orders after a date (no repeat purchase table available) |

### E-commerce
| Abbreviation | Meaning | How to query |
|---|---|---|
| SKU | Stock Keeping Unit | `product_id` in `olist_products` |
| CR / CVR | Conversion Rate | Not available — no traffic/session data |
| CTR | Click Through Rate | Not available — no traffic data |
| UPT | Units Per Transaction | `item_count` from `vw_orders_metrics` or `COUNT(order_item_id)/COUNT(DISTINCT order_id)` from `olist_order_items` |
| RFM | Recency Frequency Monetary | Recency/frequency approximated via `olist_orders`; monetary via `olist_order_items` |

---

## Unanswerable Questions (return message, no SQL)
These cannot be answered from available data:
- Individual customer behaviour over time (no customer history table)
- Seller improvement trends over time (vw_seller_metrics has no time dimension)
- Real-time inventory or stock levels (static dataset)
- Profit margins (no cost data, only revenue)
- Marketing spend or ROI (no marketing data)
- Competitor analysis (single platform dataset)
