from app.sql.connector import run_query

# ── Views ─────────────────────────────────────────────────────────────────────
print("=== vw_monthly_revenue ===")
for row in run_query("SELECT * FROM vw_monthly_revenue LIMIT 3"):
    print(row)

print("\n=== vw_orders_metrics ===")
for row in run_query("SELECT * FROM vw_orders_metrics LIMIT 3"):
    print(row)

print("\n=== vw_product_metrics ===")
for row in run_query("SELECT * FROM vw_product_metrics LIMIT 3"):
    print(row)

print("\n=== vw_seller_metrics ===")
for row in run_query("SELECT * FROM vw_seller_metrics LIMIT 3"):
    print(row)

# ── Raw tables ────────────────────────────────────────────────────────────────
print("\n=== olist_customers ===")
for row in run_query("SELECT * FROM olist_customers LIMIT 3"):
    print(row)

print("\n=== olist_order_payments ===")
for row in run_query("SELECT * FROM olist_order_payments LIMIT 3"):
    print(row)

# ── Log table ─────────────────────────────────────────────────────────────────
print("\n=== text2insight_user_query_log ===")
rows = run_query("SELECT COUNT(*) AS total_rows FROM default.text2insight_user_query_log")
print(f"  Row count: {rows[0]['total_rows']}")
