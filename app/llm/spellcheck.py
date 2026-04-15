"""
spellcheck.py — Phase 9 addition
Corrects spelling and casual abbreviations in user prompts before they
enter the main pipeline.

Uses pyspellchecker (edit-distance + word frequency) for automatic typo
correction. A minimal numeric-shorthand map handles the only cases a
dictionary-based checker genuinely cannot infer ("4"→"for", "2"→"to").

No API calls — runs fully offline and never blocks the pipeline.

Usage:
    from app.llm.spellcheck import correct_prompt
    clean_text = correct_prompt(raw_text)
"""

import re
from spellchecker import SpellChecker

_spell = SpellChecker()

# Domain terms that must never be "corrected"
_DOMAIN_WORDS = {
    "avg", "max", "min", "sum", "count", "top", "by", "vs",
    "revenue", "seller", "sellers", "sku", "skus", "order", "orders",
    "delivery", "deliveries", "category", "categories",
}
_spell.word_frequency.load_words(_DOMAIN_WORDS)

# Numeric/symbolic shorthand — the only cases pyspellchecker can't auto-fix
_NUMERIC_SHORTHAND = {
    "4": "for",
    "2": "to",
    "b4": "before",
    # Words where edit-distance-1 produces a wrong correction —
    # pyspellchecker can't recover these without explicit entries
    "averg": "average",
    "averag": "average",
    "delivry": "delivery",
    "dlvry": "delivery",
    "revenu": "revenue",
    "reveneu": "revenue",
}

# Symbol substitutions applied before tokenising — these are punctuation/operators
# that pyspellchecker never sees (they don't appear in word tokens)
_SYMBOL_SUBS = [
    (re.compile(r'\s*&\s*'),  ' and '),
    (re.compile(r'\s*%\s*'),  ' percent '),
    (re.compile(r'\s*>\s*'),  ' greater than '),
    (re.compile(r'\s*<\s*'),  ' less than '),
]


def correct_prompt(text: str) -> str:
    """
    Returns a spelling-corrected version of `text`.
    Splits the input into word/non-word tokens, corrects each word token
    via pyspellchecker (with numeric shorthand as a pre-pass), and
    reassembles the string preserving punctuation and spacing.
    """
    if not text or not text.strip():
        return text

    # Expand symbols before word-level tokenisation
    for pattern, replacement in _SYMBOL_SUBS:
        text = pattern.sub(replacement, text)
    text = text.strip()

    tokens = re.findall(r"[A-Za-z0-9']+|[^A-Za-z0-9']+", text)
    corrected_tokens = []

    for token in tokens:
        # Non-word token (spaces, punctuation) — pass through unchanged
        if not re.match(r"[A-Za-z0-9']", token):
            corrected_tokens.append(token)
            continue

        lower = token.lower()

        # Numeric shorthand ("4", "2", "b4") — dictionary can't infer these
        if lower in _NUMERIC_SHORTHAND:
            replacement = _NUMERIC_SHORTHAND[lower]
            corrected_tokens.append(replacement)
            continue

        # Pure numbers and whitelisted domain terms — leave untouched
        if token.isdigit() or lower in _DOMAIN_WORDS:
            corrected_tokens.append(token)
            continue

        # pyspellchecker: edit-distance + word-frequency correction
        candidate = _spell.correction(lower)
        if candidate and candidate != lower:
            # Preserve capitalisation of the original token
            if token[0].isupper():
                candidate = candidate.capitalize()
            corrected_tokens.append(candidate)
        else:
            corrected_tokens.append(token)

    corrected = "".join(corrected_tokens)

    if corrected.lower() != text.lower():
        print(f"[Spellcheck] '{text}' → '{corrected}'")

    return corrected
