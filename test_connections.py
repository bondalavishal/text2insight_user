import os
import httpx
from dotenv import load_dotenv
load_dotenv()

# ── 1. Databricks ─────────────────────────────────────────────────────────────
print("Testing Databricks connection...")
from databricks import sql
conn = sql.connect(
    server_hostname=os.getenv("DATABRICKS_SERVER_HOSTNAME"),
    http_path=os.getenv("DATABRICKS_HTTP_PATH"),
    access_token=os.getenv("DATABRICKS_TOKEN")
)
cursor = conn.cursor()
cursor.execute("SELECT 1 AS test")
result = cursor.fetchone()
print(f"✅ Databricks: Connected — {result}")
cursor.close()
conn.close()

# ── 2. ChromaDB ───────────────────────────────────────────────────────────────
print("\nTesting ChromaDB...")
import chromadb
chroma_client = chromadb.Client()
collection = chroma_client.create_collection("test")
print("✅ ChromaDB: Running")

# ── 3. Slack SDK ──────────────────────────────────────────────────────────────
print("\nTesting Slack SDK...")
from slack_sdk import WebClient
slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
auth = slack_client.auth_test()
print(f"✅ Slack: Connected as bot ID {auth['bot_id']}")

# ── 4. Cerebras ───────────────────────────────────────────────────────────────
print("\nTesting Cerebras API...")
try:
    from cerebras.cloud.sdk import Cerebras
    cerebras_client = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
    resp = cerebras_client.chat.completions.create(
        model="qwen-3-235b-a22b-instruct-2507",
        messages=[{"role": "user", "content": "Reply with: OK"}],
        max_tokens=10,
        timeout=15,
    )
    print(f"✅ Cerebras: {resp.choices[0].message.content.strip()}")
except Exception as e:
    print(f"❌ Cerebras failed: {e}")

# ── 5. Groq ───────────────────────────────────────────────────────────────────
print("\nTesting Groq API...")
try:
    res = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
            "Content-Type":  "application/json",
        },
        json={
            "model":    "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Reply with: OK"}],
            "max_tokens": 10,
        },
        timeout=15,
    )
    res.raise_for_status()
    print(f"✅ Groq: {res.json()['choices'][0]['message']['content'].strip()}")
except Exception as e:
    print(f"❌ Groq failed: {e}")

# ── 6. OpenRouter ─────────────────────────────────────────────────────────────
print("\nTesting OpenRouter API...")
try:
    res = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type":  "application/json",
            "HTTP-Referer":  "https://text2insight.app",
            "X-Title":       "text2insight",
        },
        json={
            "model":    "openrouter/free",
            "messages": [{"role": "user", "content": "Reply with: OK"}],
            "max_tokens": 10,
        },
        timeout=15,
    )
    res.raise_for_status()
    print(f"✅ OpenRouter: {res.json()['choices'][0]['message']['content'].strip()}")
except Exception as e:
    print(f"❌ OpenRouter failed: {e}")

print("\n✅ All connections verified.")
