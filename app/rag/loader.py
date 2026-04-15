"""
RAG loader — re-run whenever RAG docs change.

Chunking strategy:
- schema / metric docs: split on ## headings (each section = one chunk)
- business_logic.md:    split on ### PATTERN headings (each pattern = one chunk)
  This ensures every pattern gets its own embedding and can be retrieved
  independently — previously all patterns were one massive ## chunk.

Usage:
    cd /Users/vishalbondala/Applications/insightbot
    source venv/bin/activate
    python -m app.rag.loader
"""

import os
import re
import chromadb
from chromadb.utils import embedding_functions

RAG_DIR    = os.path.dirname(__file__)
CHROMA_DIR = os.path.join(RAG_DIR, "chroma_db")

DOCS = {
    "schema":         os.path.join(RAG_DIR, "schema_definitions.md"),
    "metrics":        os.path.join(RAG_DIR, "metric_definitions.md"),
    "business_logic": os.path.join(RAG_DIR, "business_logic.md"),
}


def _chunk_on_heading(text: str, source: str, heading_prefix: str) -> list[dict]:
    """Split text on lines starting with `heading_prefix`. Each section = one chunk."""
    chunks = []
    current_heading = "intro"
    current_lines   = []

    for line in text.splitlines():
        if line.startswith(heading_prefix):
            if current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    chunks.append({"heading": current_heading, "content": content, "source": source})
            current_heading = line.lstrip("# ").strip()
            current_lines   = [line]  # include the heading line in the chunk content
        else:
            current_lines.append(line)

    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            chunks.append({"heading": current_heading, "content": content, "source": source})

    return chunks


def chunk_markdown(text: str, source: str) -> list[dict]:
    """
    For business_logic: chunk at ### level so each pattern is its own document.
    For other docs: chunk at ## level (section-level).
    """
    if source == "business_logic":
        # First split out the preamble sections (## headings before patterns)
        # then split the pattern library on ### headings
        preamble_chunks = []
        pattern_text    = text

        # Extract preamble (everything before the first ### PATTERN)
        first_pattern = re.search(r'^### PATTERN', text, re.MULTILINE)
        if first_pattern:
            preamble_text = text[:first_pattern.start()]
            pattern_text  = text[first_pattern.start():]
            preamble_chunks = _chunk_on_heading(preamble_text, source, "## ")

        pattern_chunks = _chunk_on_heading(pattern_text, source, "### ")
        return preamble_chunks + pattern_chunks

    return _chunk_on_heading(text, source, "## ")


def load():
    print("RAG loader starting...")

    ef     = embedding_functions.ONNXMiniLM_L6_V2()
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    try:
        client.delete_collection("insightbot_rag")
        print("  Deleted existing collection.")
    except Exception:
        pass

    collection = client.create_collection(
        name="insightbot_rag",
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    all_chunks = []
    for source, path in DOCS.items():
        with open(path, "r") as f:
            text = f.read()
        chunks = chunk_markdown(text, source)
        all_chunks.extend(chunks)
        print(f"  {source}: {len(chunks)} chunks")

    collection.add(
        ids       = [f"{c['source']}_{i}" for i, c in enumerate(all_chunks)],
        documents = [f"{c['heading']}\n\n{c['content']}" for c in all_chunks],
        metadatas = [{"source": c["source"], "heading": c["heading"]} for c in all_chunks],
    )

    print(f"\n✅ Loaded {len(all_chunks)} chunks into ChromaDB")
    print("Re-run only if RAG docs change.")


if __name__ == "__main__":
    load()
