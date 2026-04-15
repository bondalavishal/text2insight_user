"""
Phase 6 — Semantic Cache
Uses ChromaDB (separate collection from RAG) + all-MiniLM-L6-v2.
Stores question+answer pairs. Returns cached answer if similarity >= threshold.
No expiry — dataset is static (Olist 2016-2018).
"""

import os
import re
import time
import chromadb
from chromadb.utils import embedding_functions
from app.sql.connector import run_query

_NUMBERS_RE = re.compile(r'\b\d+\b')

RAG_DIR    = os.path.join(os.path.dirname(__file__), '..', 'rag')
CHROMA_DIR = os.path.join(RAG_DIR, "chroma_db")

SIMILARITY_THRESHOLD = 0.78  # cosine similarity — lower = more cache hits on paraphrased questions

_ef = embedding_functions.ONNXMiniLM_L6_V2()

_client     = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
        try:
            _collection = _client.get_collection(
                name="insightbot_cache",
                embedding_function=_ef,
            )
        except Exception:
            _collection = _client.create_collection(
                name="insightbot_cache",
                embedding_function=_ef,
                metadata={"hnsw:space": "cosine"},
            )
    return _collection


def _numbers_match(query: str, cached: str) -> bool:
    """
    Returns False if the query contains specific numbers that differ from
    those in the cached question — e.g. '2018' vs '2017', 'top 5' vs 'top 10'.
    Prevents year/value cross-contamination in cache hits.

    Logic:
    - If the query has no numbers → allow (general question, no filter to enforce)
    - If the query has numbers but the cached question has different (or no) numbers → reject
    - If both have the same numbers → allow
    """
    query_nums  = set(_NUMBERS_RE.findall(query.lower()))
    cached_nums = set(_NUMBERS_RE.findall(cached.lower()))
    if not query_nums:
        return True          # query is number-free — no guard needed
    return query_nums == cached_nums


def _db_lookup_log_id(cached_question: str) -> int | None:
    """
    For cache entries saved before log_id was stored in ChromaDB metadata,
    look up the original log_id from Databricks using the stored question text.
    Only called once per old entry — result is backfilled into ChromaDB metadata.
    """
    try:
        esc = cached_question.replace("'", "''")
        rows = run_query(
            f"SELECT log_id FROM default.insightbot_interactions "
            f"WHERE question_asked = '{esc}' AND self_learned = TRUE "
            f"ORDER BY ts DESC LIMIT 1"
        )
        if rows:
            log_id = rows[0].get("log_id")
            if log_id:
                print(f"[Cache] Backfilled log_id={log_id} for: {cached_question[:60]}...")
            return log_id
    except Exception as e:
        print(f"[Cache] DB lookup for log_id failed: {e}")
    return None


def get_cached(question: str) -> dict | None:
    """
    Check if a semantically similar question exists in the cache.
    Returns {"answer": str, "sql": str, "similarity": float} or None.
    """
    try:
        collection = _get_collection()
        if collection.count() == 0:
            return None

        results = collection.query(
            query_texts=[question],
            n_results=1,
            include=["documents", "metadatas", "distances"],
        )

        if not results["documents"][0]:
            return None

        distance   = results["distances"][0][0]
        similarity = round(1 - distance, 4)
        meta       = results["metadatas"][0][0]

        if similarity >= SIMILARITY_THRESHOLD:
            cached_question = results["documents"][0][0]
            if not _numbers_match(question, cached_question):
                print(
                    f"[Cache] REJECTED — number mismatch "
                    f"(query={set(_NUMBERS_RE.findall(question.lower()))} "
                    f"cached={set(_NUMBERS_RE.findall(cached_question.lower()))}) "
                    f"for: {question[:60]}..."
                )
                return None
            print(f"[Cache] HIT (similarity={similarity}) for: {question[:60]}...")
            raw_log_id = meta.get("log_id")

            # Fallback for old cache entries that predate log_id storage:
            # look up the original log_id from Databricks and backfill into ChromaDB
            # so future hits are instant (no DB call needed again).
            if not raw_log_id:
                raw_log_id = _db_lookup_log_id(cached_question)
                if raw_log_id:
                    update_cache_log_id(cached_question, int(raw_log_id))

            return {
                "answer":                meta.get("answer", ""),
                "sql":                   meta.get("sql", ""),
                "csv_string":            meta.get("csv_string", ""),
                "result_json":           meta.get("result_json", ""),
                "similarity":            similarity,
                "similarity_matched_id": int(raw_log_id) if raw_log_id else None,
            }

        print(f"[Cache] MISS (similarity={similarity}) for: {question[:60]}...")
        return None

    except Exception as e:
        print(f"[Cache] Error during lookup: {e}")
        return None


def save_to_cache(question: str, answer: str, sql: str, csv_string: str = "", result_json: str = "") -> None:
    """
    Save a question+answer pair to the cache, including the CSV and raw
    result_json so that cache hits can re-run anomaly detection dynamically.
    """
    try:
        collection = _get_collection()
        cache_id = f"cache_{abs(hash(question.lower().strip()))}"

        # Delete existing entry for this question if present (upsert behaviour)
        try:
            collection.delete(ids=[cache_id])
        except Exception:
            pass

        collection.add(
            ids       = [cache_id],
            documents = [question],
            metadatas = [{"answer": answer, "sql": sql,
                          "csv_string": csv_string, "result_json": result_json or "",
                          "cached_at": str(time.time())}],
        )
        print(f"[Cache] Saved: {question[:60]}...")

    except Exception as e:
        print(f"[Cache] Error during save: {e}")


def update_cache_log_id(question: str, log_id: int) -> None:
    """
    Attach the Databricks log_id to an existing cache entry so that future
    cache hits can populate similarity_matched_id in the interaction log.
    Called after log_interaction() returns the log_id for a successful query.
    """
    try:
        collection = _get_collection()
        cache_id   = f"cache_{abs(hash(question.lower().strip()))}"
        existing   = collection.get(ids=[cache_id])
        if existing and existing["metadatas"]:
            meta = existing["metadatas"][0].copy()
            meta["log_id"] = str(log_id)
            collection.update(ids=[cache_id], metadatas=[meta])
    except Exception as e:
        print(f"[Cache] Failed to attach log_id to cache entry: {e}")


def evict_from_cache(question: str) -> bool:
    """
    Remove a question's cached answer from ChromaDB.
    Called when a user signals a negative reaction so the bad answer
    is never served again to future similar questions.
    Returns True if the entry was found and deleted.
    """
    try:
        collection = _get_collection()
        cache_id   = f"cache_{abs(hash(question.lower().strip()))}"
        collection.delete(ids=[cache_id])
        print(f"[Cache] Evicted: {question[:60]}...")
        return True
    except Exception as e:
        print(f"[Cache] Error during eviction: {e}")
        return False


def cache_stats() -> dict:
    """Return cache size and collection info."""
    try:
        collection = _get_collection()
        return {"total_cached": collection.count()}
    except Exception:
        return {"total_cached": 0}
