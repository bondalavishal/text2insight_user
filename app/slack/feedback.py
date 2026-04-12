"""
Phase 8 — Feedback Handler
Classifies user signals from text replies.

Short messages like "yes", "wrong", "thanks" after a bot answer
are treated as positive or negative feedback.

Signal values: 'positive' | 'negative'

Positive → success_signal written to Databricks.
Negative → success_signal written to Databricks + bad answer evicted from ChromaDB
           so the bot never serves it again.
"""

import re

# ── Text reply patterns ───────────────────────────────────────────────────────
# Only fires on short messages (≤ 10 words) so real questions aren't captured.

_POSITIVE_PATTERNS = [
    r"\byes\b", r"\byep\b", r"\byeah\b", r"\bcorrect\b",
    r"\bperfect\b", r"\bgreat\b", r"\bthanks\b", r"\bthank you\b",
    r"\bhelpful\b", r"\bright\b", r"\bexactly\b", r"\bawesome\b",
    r"\bnice\b", r"\bgood\b", r"\bworks\b", r"\bconfirmed\b",
]

_NEGATIVE_PATTERNS = [
    r"\bno\b", r"\bnope\b", r"\bwrong\b", r"\bincorrect\b",
    r"\bbad\b", r"\buseless\b", r"\bnot right\b", r"\bnot helpful\b",
    r"\bdoesn't look right\b", r"\bdoesn't seem right\b",
    r"\bthat's wrong\b", r"\bthat's not\b", r"\binaccurate\b",
]


def classify_feedback_text(text: str) -> str | None:
    """
    Returns 'positive', 'negative', or None if the message isn't feedback.

    Rules:
      - Message must be ≤ 10 words (longer = likely a new question)
      - Negative patterns checked first (more specific)
    """
    if not text:
        return None
    if len(text.strip().split()) > 10:
        return None

    t = text.lower().strip()

    for pattern in _NEGATIVE_PATTERNS:
        if re.search(pattern, t):
            return "negative"

    for pattern in _POSITIVE_PATTERNS:
        if re.search(pattern, t):
            return "positive"

    return None
