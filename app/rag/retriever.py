"""
Query library retriever + self-learning writer.

retrieve()      — returns few-shot Q+SQL examples from ChromaDB for a given question.
learn_pattern() — called after every fresh successful query; inserts into ChromaDB
                  and backfills embedding_id on default.text2insight_user_query_log.
"""

import os
import threading
import chromadb
from datetime import datetime
from app.utils import quiet_macos

_ts = lambda: datetime.now().strftime("%H:%M:%S")
from chromadb.utils import embedding_functions

RAG_DIR         = os.path.dirname(__file__)
CHROMA_DIR      = os.path.join(RAG_DIR, "chroma_db")
COLLECTION_NAME = "text2insight_query_lib"

_ef = embedding_functions.ONNXMiniLM_L6_V2()

_client     = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
        try:
            _collection = _client.get_collection(
                name=COLLECTION_NAME,
                embedding_function=_ef,
            )
        except Exception:
            _collection = _client.create_collection(
                name=COLLECTION_NAME,
                embedding_function=_ef,
                metadata={"hnsw:space": "cosine"},
            )
    return _collection


def retrieve(question: str, top_k: int = 5, min_relevance: float = 0.5) -> str:
    """
    Returns the top_k most similar Q+SQL pairs as formatted few-shot examples,
    ready to inject into the SQL prompt. Returns empty string when the library
    is empty (cold start) or ChromaDB is unavailable.
    """
    try:
        collection = _get_collection()
        count = collection.count()
        if count == 0:
            return ""

        n = min(top_k, count)
        with quiet_macos():
            results = collection.query(
                query_texts=[question],
                n_results=n,
                include=["documents", "metadatas", "distances"],
            )

        docs      = results["documents"][0]
        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        chunks = []
        for i, (doc, meta, dist) in enumerate(zip(docs, metadatas, distances)):
            relevance = round(1 - dist, 3)
            if relevance < min_relevance:
                continue
            chunks.append(f"[Example {i+1} | similarity: {relevance}]\n{doc}")

        if not chunks:
            return ""

        print(f"{_ts()} [QueryLib] {len(chunks)} examples retrieved for: {question[:60]}...")
        return "\n\n---\n\n".join(chunks)

    except Exception as e:
        print(f"{_ts()} [QueryLib] ChromaDB unavailable ({e})")
        return ""


_learn_lock = threading.Lock()


def learn_pattern(question: str, sql: str, log_id: int = None) -> None:
    """
    Inserts a new Q+SQL pair into ChromaDB and backfills embedding_id on the
    interaction log row. Skips if a near-duplicate already exists (similarity >= 0.97).
    Called in a background thread after every fresh successful query.
    """
    def _write():
        try:
            collection = _get_collection()

            # Deduplicate — skip if a very similar question already exists
            count = collection.count()
            if count > 0:
                with quiet_macos():
                    results = collection.query(
                        query_texts=[question],
                        n_results=1,
                        include=["distances"],
                    )
                if results["distances"][0]:
                    similarity = round(1 - results["distances"][0][0], 4)
                    if similarity >= 0.97:
                        print(f"{_ts()} [QueryLib] Skipping — near-duplicate exists (similarity={similarity})")
                        return

            with _learn_lock:
                doc_text = f'Question: "{question}"\n\nSQL:\n{sql}'
                doc_id   = f"query_{abs(hash(question.lower().strip()))}"

                with quiet_macos():
                    collection.add(
                        ids       = [doc_id],
                        documents = [doc_text],
                        metadatas = [{"question": question[:500], "feedback": ""}],
                    )

                # Backfill embedding_id on the interaction log row
                if log_id:
                    try:
                        from app.eval.interaction_logger import update_embedding_id
                        update_embedding_id(log_id, doc_id)
                    except Exception as db_err:
                        print(f"{_ts()} [QueryLib] embedding_id backfill failed: {db_err}")

            print(f"{_ts()} [QueryLib] Learned: {question[:60]}...")

        except Exception as e:
            print(f"{_ts()} [QueryLib] learn_pattern failed: {e}")

    threading.Thread(target=_write, daemon=True).start()
