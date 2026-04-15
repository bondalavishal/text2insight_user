"""
intent.py — LLM-based intent + feedback classifier.

Replaces the previous regex pattern approach with a single Cerebras call
that classifies every message into one of five intents:

  greeting         — hello, hi, who are you, etc.
  text_to_sql      — data question to be answered via SQL
  feedback_positive — user confirming the last answer was correct (yes, thanks, etc.)
  feedback_negative — user saying the last answer was wrong (no, wrong, etc.)
  out_of_scope     — anything unrelated to business data

Combining intent + feedback into one call removes all hardcoded word lists
and handles edge cases like "good delivery time" (text_to_sql) vs
"good answer" (feedback_positive) naturally.
"""

import os
from cerebras.cloud.sdk import Cerebras

CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"
_cerebras = None


def _get_client():
    global _cerebras
    if _cerebras is None:
        _cerebras = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
    return _cerebras

_PROMPT = """You are a message classifier for a business analytics Slack bot.

Classify the user message into exactly one of these intents:
- greeting        : greetings, introductions, asking what the bot can do
- text_to_sql     : any question about business data (orders, revenue, delivery, sellers, products, categories, states)
- feedback_positive: user confirming the last bot answer was correct or helpful (e.g. "yes", "correct", "thanks", "that's right")
- feedback_negative: user saying the last bot answer was wrong or unhelpful (e.g. "no", "wrong", "that's not right", "incorrect")
- out_of_scope    : anything unrelated to business data and not a greeting or feedback

Rules:
- If the message contains a data question (even with typos or casual language), classify as text_to_sql
- Short one-word or two-word affirmatives after a bot answer are feedback_positive
- Short one-word or two-word negatives after a bot answer are feedback_negative
- "good delivery time" or "good revenue" → text_to_sql (the word "good" is part of the question)
- "good" or "great" or "nice" alone as a reply → feedback_positive
- When in doubt between text_to_sql and feedback, prefer text_to_sql

Reply with ONLY the intent label, nothing else.

Message: {text}
Intent:"""


def classify_intent(text: str) -> str:
    """
    Returns one of: greeting | text_to_sql | feedback_positive | feedback_negative | out_of_scope
    Falls back to text_to_sql if Cerebras is unavailable.
    """
    if not text or not text.strip():
        return "text_to_sql"

    try:
        response = _get_client().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[{"role": "user", "content": _PROMPT.format(text=text)}],
            temperature=0,
            max_tokens=10,
            timeout=10,
        )
        label = response.choices[0].message.content.strip().lower()
        if label in ("greeting", "text_to_sql", "feedback_positive", "feedback_negative", "out_of_scope"):
            return label
        return "text_to_sql"
    except Exception as e:
        print(f"[Intent] Cerebras unavailable ({e}) — defaulting to text_to_sql")
        return "text_to_sql"
