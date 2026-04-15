"""
RAG retriever + self-learning pattern writer.

retrieve()      — called by sql_generator.py on every question.
learn_pattern() — called after every fresh successful query to append the
                  Q+SQL pair to business_logic.md and insert it into the
                  live ChromaDB collection so it is immediately retrievable.
"""

import os
import re
import threading
import chromadb
from chromadb.utils import embedding_functions

RAG_DIR    = os.path.dirname(__file__)
CHROMA_DIR = os.path.join(RAG_DIR, "chroma_db")

_ef = embedding_functions.ONNXMiniLM_L6_V2()

_client     = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
        _collection = _client.get_collection(
            name="insightbot_rag",
            embedding_function=_ef,
        )
    return _collection


def retrieve(question: str, top_k: int = 15, min_relevance: float = 0.3) -> str:
    """
    Returns the top_k most relevant RAG chunks as a formatted string
    ready to inject into the SQL prompt.
    Falls back to minimal hardcoded schema if ChromaDB is unavailable.

    top_k increased to 15 so that with per-pattern chunking, multiple
    relevant patterns are retrieved rather than a single large section.
    min_relevance filters out noise below 0.3 cosine similarity.
    """
    try:
        collection = _get_collection()
        results = collection.query(
            query_texts=[question],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        docs      = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        chunks = []
        for doc, meta, dist in zip(docs, metadatas, distances):
            relevance = round(1 - dist, 3)
            if relevance < min_relevance:
                continue
            source = meta.get("source", "unknown")
            chunks.append(f"[{source} | relevance: {relevance}]\n{doc}")

        context = "\n\n---\n\n".join(chunks)
        print(f"[RAG] {len(chunks)} chunks retrieved for: {question[:60]}...")
        return context

    except Exception as e:
        print(f"[RAG] ChromaDB unavailable ({e}), using fallback schema.")
        return _FALLBACK_SCHEMA


BUSINESS_LOGIC_PATH = os.path.join(RAG_DIR, "business_logic.md")
_learn_lock = threading.Lock()


def _next_pattern_number() -> int:
    """Count existing ### PATTERN headings to get the next pattern number."""
    try:
        with open(BUSINESS_LOGIC_PATH, "r") as f:
            content = f.read()
        return len(re.findall(r'^### PATTERN \d+', content, re.MULTILINE)) + 1
    except Exception:
        return 1


def learn_pattern(question: str, sql: str) -> None:
    """
    Appends a new pattern to business_logic.md and inserts it directly
    into the live ChromaDB RAG collection — no full reload needed.

    Called in a background thread after every fresh successful query so
    future similar questions retrieve this SQL pattern automatically.

    Skips if an identical question already exists in the RAG collection
    (similarity >= 0.97) to avoid near-duplicate patterns.
    """
    def _write():
        try:
            collection = _get_collection()

            # Deduplicate — skip if a very similar question is already in RAG
            results = collection.query(
                query_texts=[question],
                n_results=1,
                include=["distances"],
            )
            if results["distances"][0]:
                similarity = round(1 - results["distances"][0][0], 4)
                if similarity >= 0.97:
                    print(f"[RAG] Skipping learn — near-duplicate exists (similarity={similarity})")
                    return

            with _learn_lock:
                pattern_num = _next_pattern_number()
                heading     = f"PATTERN {pattern_num} — Learned from user query"
                chunk_text  = (
                    f"### {heading}\n"
                    f"Question: \"{question}\"\n"
                    f"```sql\n{sql}\n```"
                )

                # Append to business_logic.md
                with open(BUSINESS_LOGIC_PATH, "a") as f:
                    f.write(f"\n\n{chunk_text}\n")

                # Insert directly into live ChromaDB collection
                chunk_id = f"learned_{pattern_num}_{abs(hash(question.lower().strip()))}"
                collection.add(
                    ids       = [chunk_id],
                    documents = [f"{heading}\n\n{chunk_text}"],
                    metadatas = [{"source": "business_logic", "heading": heading, "learned": "true"}],
                )

            print(f"[RAG] Learned PATTERN {pattern_num}: {question[:60]}...")

        except Exception as e:
            print(f"[RAG] learn_pattern failed: {e}")

    threading.Thread(target=_write, daemon=True).start()


_FALLBACK_SCHEMA = """
CREATE VIEW vw_monthly_revenue AS SELECT
    year INT, month INT, year_month STRING,
    total_orders INT, total_revenue DECIMAL,
    avg_order_value DECIMAL, unique_customers INT, canceled_orders INT FROM ...;

CREATE VIEW vw_orders_metrics AS SELECT
    order_id STRING, customer_id STRING, order_status STRING,
    order_date DATE, order_year INT, order_month INT,
    customer_city STRING, customer_state STRING,
    order_revenue DECIMAL, order_freight DECIMAL, order_total DECIMAL,
    item_count INT, delivery_days INT FROM ...;

CREATE VIEW vw_product_metrics AS SELECT
    product_id STRING, category STRING, product_weight_g INT,
    total_orders INT, total_revenue DECIMAL,
    avg_price DECIMAL, avg_review_score DECIMAL FROM ...;

CREATE VIEW vw_seller_metrics AS SELECT
    seller_id STRING, seller_city STRING, seller_state STRING,
    total_orders INT, total_revenue DECIMAL, avg_order_value DECIMAL,
    unique_products INT, avg_review_score DECIMAL, total_reviews INT FROM ...;

RULES: Use views by default. Use raw tables only when views cannot answer. NEVER join views to each other. NEVER invent columns. Always LIMIT.
"""
