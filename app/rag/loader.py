"""
Query library loader — bootstraps ChromaDB from two sources:
  1. schema_definitions.md  — hand-crafted Q+SQL patterns (always seeded first)
  2. Databricks query log   — real successful queries from production usage

Run manually to rebuild ChromaDB (e.g. after a fresh deploy):
    python -m app.rag.loader
"""

import os
import re
import chromadb
from chromadb.utils import embedding_functions

RAG_DIR         = os.path.dirname(__file__)
CHROMA_DIR      = os.path.join(RAG_DIR, "chroma_db")
COLLECTION_NAME = "text2insight_query_lib"
SCHEMA_FILE     = os.path.join(RAG_DIR, "schema_definitions.md")


def _parse_schema_patterns(path: str) -> list[dict]:
    """
    Extracts Q+SQL pairs from schema_definitions.md.
    Looks for blocks of the form:
        ### <description>:
        ```sql
        <SQL>
        ```
    The description becomes the "question" stub and the SQL block is the answer.
    """
    pairs = []
    with open(path) as f:
        text = f.read()

    # Find every ### heading followed by a ```sql ... ``` block
    pattern = re.compile(
        r'###\s+([^\n:]+)[:\n][^\n]*\n'   # ### heading line
        r'(?:[^\n]*\n)*?'                  # optional comment lines (rule annotations)
        r'```sql\s*\n(.*?)```',            # sql block
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        heading = m.group(1).strip()
        sql     = m.group(2).strip()
        if sql and len(sql) > 20:
            pairs.append({"question": heading, "sql": sql})

    return pairs


def load():
    print("Query library loader starting...")

    ef     = embedding_functions.ONNXMiniLM_L6_V2()
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    try:
        client.delete_collection(COLLECTION_NAME)
        print("  Deleted existing collection.")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    ids, docs, metas = [], [], []
    seen_ids: set = set()

    # ── 1. Seed from schema_definitions.md ───────────────────────────────────
    schema_pairs = _parse_schema_patterns(SCHEMA_FILE)
    for pair in schema_pairs:
        q   = pair["question"]
        sql = pair["sql"]
        doc_id = f"schema_{abs(hash(q.lower().strip()))}"
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        ids.append(doc_id)
        docs.append(f'Question: "{q}"\n\nSQL:\n{sql}')
        metas.append({"question": q, "feedback": "", "source": "schema_definitions"})

    print(f"  Seeded {len(ids)} patterns from schema_definitions.md.")

    # ── 2. Bootstrap from Databricks text2insight_user_query_log ────────────
    db_count = 0
    try:
        from app.sql.connector import run_query
        rows = run_query("""
            SELECT log_id, question_asked AS question, generated_sql AS sql, success_signal AS feedback
            FROM default.text2insight_user_query_log
            WHERE status = 'success'
              AND generated_sql IS NOT NULL
              AND (success_signal IS NULL OR success_signal != 'negative')
            ORDER BY log_id ASC
        """)

        for row in (rows or []):
            q        = (row.get("question") or "").strip()
            sql      = (row.get("sql") or "").strip()
            feedback = row.get("feedback") or ""
            log_id   = row.get("log_id")

            if not q or not sql:
                continue

            doc_id = f"query_{abs(hash(q.lower().strip()))}"
            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)

            ids.append(doc_id)
            docs.append(f'Question: "{q}"\n\nSQL:\n{sql}')
            metas.append({
                "question": q,
                "feedback": feedback,
                "db_log_id": str(log_id),
                "source": "query_log",
            })
            db_count += 1

    except Exception as e:
        print(f"  Could not load from Databricks ({e}) — schema patterns still seeded.")

    if ids:
        # Re-fetch collection to avoid stale reference after delete/create cycle
        collection = client.get_collection(name=COLLECTION_NAME, embedding_function=ef)
        # Add in batches of 50 to avoid ChromaDB internal state issues
        batch_size = 50
        for start in range(0, len(ids), batch_size):
            collection.add(
                ids=ids[start:start + batch_size],
                documents=docs[start:start + batch_size],
                metadatas=metas[start:start + batch_size],
            )
        print(f"  Loaded {db_count} Q+SQL pairs from text2insight_user_query_log.")
        print(f"\nTotal: {len(ids)} entries in ChromaDB collection '{COLLECTION_NAME}'.")
    else:
        print("\nNo entries loaded.")


if __name__ == "__main__":
    load()
