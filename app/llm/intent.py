"""
intent.py — Intent + feedback classifier.

Fast path: regex heuristic resolves ~90% of messages instantly (no LLM call).
Slow path: Cerebras (10s timeout) for genuinely ambiguous short messages.
Circuit breaker: after 2 failures in 5 min, Cerebras is skipped for 3 min.

Intents:
  greeting          — hi, hello, who are you, etc.
  text_to_sql       — any data question
  feedback_positive — confirming the last answer was correct
  feedback_negative — saying the last answer was wrong
  out_of_scope      — unrelated to business data
"""

import os
import re
from datetime import datetime
from cerebras.cloud.sdk import Cerebras
from app.llm.cerebras_breaker import is_open, record_failure, record_success

_ts = lambda: datetime.now().strftime("%H:%M:%S")

CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"
_cerebras = None


def _get_client():
    global _cerebras
    if _cerebras is None:
        _cerebras = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
    return _cerebras


# ── Heuristic patterns ────────────────────────────────────────────────────────
_GREETING_RE = re.compile(
    r'\b(hi|hello|hey|howdy|good\s+morning|good\s+afternoon|good\s+evening|'
    r'who are you|what can you do|what do you do|introduce yourself|'
    r'what\'?s? text2insight|what\'?s? your name)\b',
    re.I,
)
_FEEDBACK_POS_RE = re.compile(
    r'^(yes|yeah|yep|yup|correct|right|true|good|great|perfect|'
    r'thanks|thank you|thank u|awesome|nice|got it|understood|confirmed|exactly|'
    r'looks good|that\'?s?\s+(right|correct|good|perfect))\s*[.!]?$',
    re.I,
)
_FEEDBACK_NEG_RE = re.compile(
    r'^(no|nope|wrong|incorrect|not right|not correct|bad|off|'
    r'that\'?s?\s+(wrong|not right|incorrect|bad)|'
    r'rerun|try again|redo|different|change it)\s*[.!]?$',
    re.I,
)
_OOS_RE = re.compile(
    r'\b(weather|cricket|football|soccer|stock price|crypto|bitcoin|news|'
    r'politics|recipe|movie|sport|covid|joke|poem|story|write me|'
    r'draw|image|photo|song|music)\b',
    re.I,
)


def _heuristic(text: str) -> str | None:
    """
    Returns an intent string when confident, None when ambiguous (→ call LLM).

    Logic:
    - Greeting patterns → greeting (any length)
    - OOS keywords      → out_of_scope (any length)
    - Short (≤4 words)  → check feedback patterns; None if no match (ambiguous)
    - Long  (≥5 words)  → text_to_sql (long messages are almost always data questions)
    """
    t = text.strip()
    if _GREETING_RE.search(t):
        return "greeting"
    if _OOS_RE.search(t):
        return "out_of_scope"
    words = t.split()
    if len(words) <= 4:
        if _FEEDBACK_POS_RE.match(t):
            return "feedback_positive"
        if _FEEDBACK_NEG_RE.match(t):
            return "feedback_negative"
        return None   # short + ambiguous → LLM
    return "text_to_sql"


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
    """
    if not text or not text.strip():
        return "text_to_sql"

    # Fast path — no LLM call needed
    fast = _heuristic(text)
    if fast is not None:
        print(f"{_ts()} [Intent] Heuristic → {fast}")
        return fast

    # Slow path — short ambiguous message, need LLM
    if is_open():
        print(f"{_ts()} [Intent] Circuit open — defaulting to text_to_sql")
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
        record_success()
        if label in ("greeting", "text_to_sql", "feedback_positive", "feedback_negative", "out_of_scope"):
            return label
        return "text_to_sql"
    except Exception as e:
        record_failure()
        print(f"{_ts()} [Intent] Cerebras unavailable ({e}) — defaulting to text_to_sql")
        return "text_to_sql"
