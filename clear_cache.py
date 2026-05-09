import chromadb
client = chromadb.PersistentClient(path='app/rag/chroma_db')
client.delete_collection('text2insight_cache')
print('Cache cleared.')

