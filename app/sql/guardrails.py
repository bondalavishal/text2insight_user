import re

BLOCKED_KEYWORDS = [
    "DROP", "DELETE", "UPDATE", "INSERT",
    "ALTER", "TRUNCATE", "CREATE", "REPLACE",
    "MERGE", "GRANT", "REVOKE"
]

ALLOWED_SOURCES = [
    # Views
    "vw_monthly_revenue",
    "vw_orders_metrics",
    "vw_product_metrics",
    "vw_seller_metrics",
    # Raw tables
    "olist_orders",
    "olist_order_items",
    "olist_products",
    "olist_order_reviews",
    "olist_sellers",
    "olist_customers",
    "olist_geolocation",
    "olist_order_payments",
    "product_category_translation",
]


def validate_sql(sql: str) -> tuple[bool, str, str]:
    """
    Returns: (is_valid, reason, failure_type)

    failure_type values:
      - "ok"                 : query is valid
      - "invalid_start"      : does not begin with SELECT or WITH
      - "blocked_keyword"    : contains a DDL / write keyword
      - "disallowed_source"  : references a table/view not in ALLOWED_SOURCES
    """
    # Strip leading SQL comments before checking start — models sometimes
    # prepend a -- comment line before the WITH/SELECT keyword
    sql_stripped = re.sub(r'^\s*(--[^\n]*\n\s*)*', '', sql.strip())
    sql_upper = sql_stripped.strip().upper()

    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        return False, "Query must start with SELECT or WITH.", "invalid_start"

    for keyword in BLOCKED_KEYWORDS:
        if re.search(r'\b' + keyword + r'\b', sql_upper):
            return False, f"Blocked keyword: {keyword}", "blocked_keyword"

    if not any(source.lower() in sql_stripped.lower() for source in ALLOWED_SOURCES):
        return False, "Query must reference an allowed view or table.", "disallowed_source"

    return True, "OK", "ok"


def enforce_limit(sql: str, max_limit: int = 500) -> str:
    if "LIMIT" not in sql.upper():
        sql = sql.rstrip() + "\nLIMIT 500"
    else:
        match = re.search(r'LIMIT\s+(\d+)', sql, re.IGNORECASE)
        if match and int(match.group(1)) > max_limit:
            sql = re.sub(r'LIMIT\s+\d+', f'LIMIT {max_limit}', sql, flags=re.IGNORECASE)
    return sql
