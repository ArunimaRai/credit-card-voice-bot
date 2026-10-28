"""Shared system-prompt / developer-fact construction.

Both the live pipeline (bot/main.py, over Pipecat/WebRTC) and the eval
harness (evals/harness.py, text-only against the OpenAI API directly) build
the prompt from the same two functions here, so an eval result is a result
about the shipped prompt, not a copy of it. What differs between the two
callers is the transport only.
"""

from datetime import date

from bot.prompts.v1 import PROMPT

# The persona and bank, from config rather than hardcoded in each line
# (voice-guide.md: "Set this in config and never hardcode it").
AGENT_NAME = "Shreya"
BANK_NAME = "Mumbai Bank"

# Recorded with every call and eval row, so results stay attributable if a
# v2 ever exists.
PROMPT_VERSION = "v1"


def _spoken_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{d.day}{suffix} {d.strftime('%B')}"


def opening_line(customer: dict) -> str:
    """The first thing said on every call. Spoken straight to TTS by
    bot/main.py rather than generated, so the customer isn't left in ~1.5s
    of silence while the LLM writes a line that never changes."""
    return f"Hello, this is {AGENT_NAME} calling from {BANK_NAME}. Am I speaking to {customer['customer_name']}?"


def amount_details(customer: dict) -> str:
    """The scripted line spoken only when the customer asks how much is due.
    Built in code, not left to the model, so every figure in it is the
    fixture's."""
    if customer["late_fee"]:
        includes = f"That includes interest, and a late payment fee of {customer['late_fee']:,} rupees."
    else:
        includes = "That includes interest."
    return (
        f"Your minimum amount due is {customer['min_due']:,} rupees. "
        f"{includes} "
        f"Your total outstanding is {customer['total_outstanding']:,} rupees."
    )


def build_system_prompt(customer: dict | None) -> str:
    """Fills the <customer_name>, <due_date> and <amount_details> placeholders."""
    if customer is None:
        return PROMPT
    return (
        PROMPT.replace("<customer_name>", customer["customer_name"])
        .replace("<due_date>", _spoken_date(customer["due_date"]))
        .replace("<amount_details>", amount_details(customer))
    )


def build_developer_message(customer: dict | None) -> str | None:
    if customer is None:
        return None
    return (
        f"Today's date is {date.today().isoformat()}. "
        f"Due date: {customer['due_date']}. Minimum amount due: {customer['min_due']} rupees. "
        f"Interest charged this cycle: {customer['interest_charged']} rupees. "
        f"Late fee: {customer['late_fee']} rupees. "
        f"Total outstanding: {customer['total_outstanding']} rupees. "
        "Share these only after the customer has confirmed their identity, and only when they ask."
    )


# --- Scripted lines (Execution Plan §16, D11-D13) -------------------------
# Spoken by code, not generated, wherever the trigger can be detected
# deterministically. The prompt quotes the same strings for the cases only
# the model can recognise, and bot/guardrails.py lets them through, so a
# scripted line never trips the rule it exists to satisfy.

# Can be needed more than once in a call, so it rotates (voice-guide.md:
# 3+ variants for any line that may be spoken more than once).
UNCLEAR_REASKS = [
    "Sorry, I didn't catch that clearly. Could you say that again?",
    "Sorry, the line broke up a little. Could you repeat that?",
    "I missed that, sorry. Could you say it once more?",
]
UNCLEAR_REASK = UNCLEAR_REASKS[0]
# Every turn is at most three sentences (voice-guide.md §13).
AUDIO_GLITCH = (
    "I'm having trouble hearing you clearly. It could be a glitch in my system. "
    "I'll arrange for a human agent to call you back."
)
IDENTITY_GLITCH = (
    "Sorry, I'm not able to verify your identity right now. It could be a glitch in my system. "
    "I'll arrange for a human agent to call you back."
)
# Spoken as the whole last turn of the call, closing included.
WAIVER_LINE = (
    "Okay, a waiver request. I'm not allowed or trained to handle those, so I'll have a human "
    "support agent call you back."
)
CLOSING_LINE = "Thanks for your time, and have a good day."
CLOSING_LINE_FULL = "Thank you for your time. Have a good day."  # the prompt's step 6 close
PRE_IDENTITY_HELP = "Of course, I can help with that. Please confirm your identity so I can proceed."

# Answers to the caller-verification questions asked before identity is
# confirmed (D12). They reveal nothing about why we're calling: not the card,
# not the debt (D5; voice-guide.md Stage 1, "without revealing why").
PRE_IDENTITY_ANSWERS = {
    "name": f"I'm {AGENT_NAME}, from {BANK_NAME}.",
    "where": f"I'm calling from {BANK_NAME}.",
    "about": f"It's about your {BANK_NAME} account. I just need to confirm who I'm speaking to first.",
    "number": f"It's the number registered with {BANK_NAME}.",
    "scam": (
        "I'll never ask for your card number, OTP or PIN. You can also call the number on the back "
        "of your card."
    ),
    "bot": f"Yes, I'm {AGENT_NAME}, an automated assistant.",
    "help": PRE_IDENTITY_HELP,
}

GLITCH_OUTCOMES = {
    "glitch_identity": {"type": "callback_request", "requested_time": "identity not verified - possible system glitch"},
    "glitch_audio": {"type": "callback_request", "requested_time": "audio unclear - possible system glitch"},
}


def identity_question(customer: dict) -> str:
    return f"Am I speaking to {customer['customer_name']}?"


def identity_reask(customer: dict) -> str:
    return f"Sorry, I didn't catch that. {identity_question(customer)}"


def scripted_lines(customer: dict | None) -> list[str]:
    """Every line the bot may say verbatim. bot/guardrails.py removes these
    from a reply before checking it, so only the model's own words are judged."""
    lines = [*UNCLEAR_REASKS, AUDIO_GLITCH, IDENTITY_GLITCH, WAIVER_LINE, CLOSING_LINE, CLOSING_LINE_FULL,
             *PRE_IDENTITY_ANSWERS.values()]
    if customer is not None:
        lines += [identity_reask(customer), identity_question(customer)]
    return lines
