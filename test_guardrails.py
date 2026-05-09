from app.sql.guardrails import validate_sql, enforce_limit
from app.llm.sql_generator import generate_sql

print("=== Guardrail Tests ===\n")

tests = [
    ("DROP TABLE vw_orders_metrics", False),
    ("DELETE FROM vw_seller_metrics WHERE 1=1", False),
    ("SELECT * FROM raw_orders LIMIT 10", False),
    ("SELECT seller_id FROM vw_seller_metrics ORDER BY total_revenue DESC LIMIT 5", True),
    ("SELECT total_revenue FROM vw_monthly_revenue LIMIT 10000", True),
]

for sql, expected in tests:
    is_valid, reason, _ = validate_sql(sql)
    enforced = enforce_limit(sql) if is_valid else sql
    status = "✅" if is_valid == expected else "❌"
    print(f"{status} valid={is_valid} | {reason}")
    if is_valid and "10000" in sql:
        print(f"   Limit enforced: {enforced}")

print("\n=== Live SQL Generation + Validation ===\n")

questions = [
    "What was total revenue last month?",
    "Which state has the most orders?",
    "Who are the top 5 sellers by revenue?",
    "What is the average delivery days by state?",
]

for q in questions:
    sql = generate_sql(q)
    is_valid, reason, _ = validate_sql(sql)
    icon = "✅" if is_valid else "❌"
    print(f"{icon} Q: {q}")
    print(f"   SQL: {sql}")
    if not is_valid:
        print(f"   BLOCKED: {reason}")
    print()
