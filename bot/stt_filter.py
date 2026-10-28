"""Junk-turn filter (Execution Plan §16.3, D11): decides whether a final
transcription is something the customer clearly said, or STT output the bot
must not act on.

Found live on 2026-10-28: short noises and echo came back as words from the
old STT hint prompt ("context:", "late fee", "waiver"), or as text in another
language ("Obrigado", Korean, Arabic). One made-up "waiver." became a
compliance escalation. An unclear turn never reaches the LLM; the bot asks
the customer to repeat instead.

The thresholds and word lists are the only part of the bot that learns from
usage (§16.7): `evals/stt_eval.py --tune` re-fits them on the live STT set
and rewrites CONFIG_PATH only when the eval metrics don't get worse. No
Pipecat dependency, so the live pipeline and the eval tools share it.
"""

import json
import re
import unicodedata
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "evals" / "live" / "stt_filter_config.json"

DEFAULT_CONFIG = {
    # Below this the model itself doubts the words (exp of mean token logprob).
    "min_confidence": 0.35,
    # A one-word turn must clear this too unless it's on the allowlist: a lone
    # domain word is exactly what a hallucination looks like.
    "single_word_min_confidence": 0.8,
    "short_answers": [
        "yes", "yeah", "yep", "yup", "ya", "haan", "han", "ha", "ji", "no", "nope", "nahi", "na",
        "ok", "okay", "hello", "hi", "speaking", "sure", "right", "correct", "fine", "thanks",
        "bye", "sorry", "what", "who", "hmm", "achha", "theek", "great", "good", "alright",
        "perfect", "done", "please", "haanji", "accha", "thik",
    ],
    # Fragments of STT prompts and formatting the old setup read back.
    "markers": ["context:", "###", "expect terms like", "collections agent", "phone call in english"],
}

# Latin (incl. accents) and Devanagari: Indian English and Hinglish come back
# in one or the other. Any other letters mean the model guessed a language.
_ALLOWED_SCRIPTS = ("LATIN", "DEVANAGARI")
# Nobody says "minimum_due" out loud (seen live as a customer turn).
_IDENTIFIER_RE = re.compile(r"\b[a-z]+_[a-z_]+\b")
_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?|\d+", re.UNICODE)


def load_config() -> dict:
    try:
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)


def _foreign_script(text: str) -> bool:
    for ch in text:
        if ch.isalpha() and not unicodedata.name(ch, "").startswith(_ALLOWED_SCRIPTS):
            return True
    return False


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def unclear_reason(text: str, *, confidence: float | None = None, config: dict | None = None) -> str | None:
    """None when the turn is clear speech, else the rule that marked it unclear."""
    config = config or load_config()
    lowered = text.lower()
    tokens = words(text)
    if not tokens:
        return "empty"
    if any(m in lowered for m in config["markers"]) or _IDENTIFIER_RE.search(text):
        return "hallucination_marker"
    if _foreign_script(text):
        return "foreign_script"
    if confidence is not None and confidence < config["min_confidence"]:
        return "low_confidence"
    if len(tokens) == 1 and tokens[0] not in config["short_answers"]:
        if confidence is None or confidence < config["single_word_min_confidence"]:
            return "lone_word"
    return None
