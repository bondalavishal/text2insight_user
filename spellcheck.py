"""
spellcheck.py — Phase 9 addition
Corrects spelling, shorthand, and casual abbreviations in user prompts
before they enter the main pipeline.

Runs via Cerebras (primary) with a silent fallback to the original text
if the API is unavailable, so the pipeline is never blocked.

Usage:
    from app.llm.spellcheck import correct_prompt
    clean_text = correct_prompt(raw_text)
"""

import os
from cerebras.cloud.sdk import Cerebras

_cerebras = Cerebras(api_key=os.getenv("CEREBRAS_API_KEY"))
CEREBRAS_MODEL = "qwen-3-235b-a22b-instruct-2507"

_SPELLCHECK_PROMPT = """You are a text correction assistant for a business analytics bot.

The user typed a message that may contain:
- Spelling mistakes
- Shorthand / abbreviations (e.g. "tym" → "time", "4" → "for", "stte" → "state")
- Casual/informal language

Your job:
1. Fix all spelling errors and shorthand while keeping the original meaning intact.
2. Return ONLY the corrected text — no explanations, no quotes, no extra words.
3. If the text is already correct, return it unchanged.
4. Do NOT change names, technical terms, or SQL keywords.

Message: {text}

Corrected:"""


def correct_prompt(text: str) -> str:
    """
    Returns a spelling-corrected version of `text`.
    Falls back to the original text silently if Cerebras is unavailable.
    """
    if not text or not text.strip():
        return text

    try:
        response = _cerebras.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[{"role": "user", "content": _SPELLCHECK_PROMPT.format(text=text)}],
            temperature=0,
            max_tokens=128,
            timeout=15,
        )
        corrected = response.choices[0].message.content.strip()
        # Sanity check: if the model returns something wildly different in length,
        # trust the original to avoid mangling intentional short commands.
        if corrected and 0.25 <= len(corrected) / max(len(text), 1) <= 4.0:
            if corrected.lower() != text.lower():
                print(f"[Spellcheck] '{text}' → '{corrected}'")
            return corrected
        return text
    except Exception as e:
        print(f"[Spellcheck] Cerebras unavailable ({e}) — using original text.")
        return text
