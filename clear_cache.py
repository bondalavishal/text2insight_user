import chromadb
client = chromadb.PersistentClient(path='app/rag/chroma_db')
client.delete_collection('insightbot_cache')
print('Cache cleared.')

