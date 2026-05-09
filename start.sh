#!/bin/bash
set -e

if [ ! -d "app/rag/chroma_db" ] || [ -z "$(ls -A app/rag/chroma_db)" ]; then
    echo "chroma_db not found — building RAG index..."
    python -m app.rag.loader
    echo "RAG index built."
else
    echo "chroma_db exists — skipping rebuild."
fi

echo "Starting text2insight..."
exec python main.py
