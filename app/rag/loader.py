"""
Query library loader — bootstraps ChromaDB from Databricks text2insight_user_query_log.

Run manually to rebuild ChromaDB from the database (e.g. after a fresh deploy):
    python -m app.rag.loader

On cold start (empty table), creates an empty collection that learn_pattern()
will populate as users ask questions.
"""

import os
import chromadb
from chromadb.utils import embedding_functions

RAG_DIR         = os.path.dirname(__file__)
CHROMA_DIR      = os.path.join(RAG_DIR, "chroma_db")
COLLECTION_NAME = "text2insight_query_lib"


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

    # Bootstrap from Databricks text2insight_user_query_log
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

        if not rows:
            print("  text2insight_user_query_log is empty — starting with empty collection.")
            print(f"\nEmpty collection '{COLLECTION_NAME}' created. Will populate as users ask questions.")
            return

        ids, docs, metas = [], [], []
        seen_ids = set()

        for row in rows:
            q        = (row.get("question") or "").strip()
            sql      = (row.get("sql") or "").strip()
            feedback = row.get("feedback") or ""
            log_id   = row.get("log_id")

            if not q or not sql:
                continue

            doc_id = f"query_{abs(hash(q.lower()))}"
            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)

            ids.append(doc_id)
            docs.append(f'Question: "{q}"\n\nSQL:\n{sql}')
            metas.append({
                "question":   q[:500],
                "feedback":   feedback,
                "db_log_id":  str(log_id),
            })

        if ids:
            collection.add(ids=ids, documents=docs, metadatas=metas)
            print(f"\nLoaded {len(ids)} Q+SQL pairs from text2insight_user_query_log into ChromaDB.")
        else:
            print("\nNo valid rows found — empty collection created.")

    except Exception as e:
        print(f"  Could not load from Databricks ({e})")
        print(f"  Starting with empty collection '{COLLECTION_NAME}'.")


if __name__ == "__main__":
    load()
