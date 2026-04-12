"""
Phase 6 — Semantic Cache
Uses ChromaDB (separate collection from RAG) + all-MiniLM-L6-v2.
Stores question+answer pairs. Returns cached answer if similarity >= threshold.
No expiry — dataset is static (Olist 2016-2018).
"""

import os
import time
import chromadb
from chromadb.utils import embedding_functions

RAG_DIR    = os.path.join(os.path.dirname(__file__), '..', 'rag')
CHROMA_DIR = os.path.join(RAG_DIR, "chroma_db")

SIMILARITY_THRESHOLD = 0.92  # cosine similarity — must be very close to match

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
            print(f"[Cache] HIT (similarity={similarity}) for: {question[:60]}...")
            return {
                "answer":     meta.get("answer", ""),
                "sql":        meta.get("sql", ""),
                "csv_string": meta.get("csv_string", ""),
                "similarity": similarity,
            }

        print(f"[Cache] MISS (similarity={similarity}) for: {question[:60]}...")
        return None

    except Exception as e:
        print(f"[Cache] Error during lookup: {e}")
        return None


def save_to_cache(question: str, answer: str, sql: str, csv_string: str = "") -> None:
    """
    Save a question+answer pair to the cache, including the CSV so that
    cache hits can offer the user a download just like a live query would.
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
                          "csv_string": csv_string, "cached_at": str(time.time())}],
        )
        print(f"[Cache] Saved: {question[:60]}...")

    except Exception as e:
        print(f"[Cache] Error during save: {e}")


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
