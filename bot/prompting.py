"""Shared system-prompt / developer-fact construction, and every line the
bot speaks verbatim.

Both the live pipeline (bot/main.py, over Pipecat/WebRTC) and the eval
harness (evals/harness.py, text-only against the OpenAI API directly) build
the prompt from the same functions here, so an eval result is a result
about the shipped prompt, not a copy of it. What differs between the two
callers is the transport only.

Every spoken line follows voice-guide.md: at most three sentences a turn,
contractions, no banned phrases, 3+ variants for lines that can repeat, and
names from config rather than hardcoded.
"""

import re
from datetime import date, datetime

from bot.prompts.v1 import PROMPT

# The persona and bank, from config rather than hardcoded in each line
# (voice-guide.md: "Set this in config and never hardcode it").
AGENT_NAME = "Shreya"
BANK_NAME = "Mumbai Bank"
# The bank is fictional, so there is no real number to quote
# (voice-guide.md Stage 7, no-agreement close).
HELPLINE = "the support number on our website"
# Tele-MANAS, India's national mental-health helpline (voice-guide.md §8).
CRISIS_LINE = "Tele-MANAS on one four four one six"

# Recorded with every call and eval row, so results stay attributable if a
# v2 ever exists.
PROMPT_VERSION = "v1"


# --- speaking numbers (voice-guide.md §4) --------------------------------

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _under_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def indian_words(n: int) -> str:
    """18420 -> "eighteen thousand four hundred twenty"; lakh and crore for
    larger amounts, the way an Indian caller says them."""
    if n == 0:
        return "zero"
    parts = []
    for size, name in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")):
        if n >= size:
            parts.append(f"{indian_words(n // size)} {name}")
            n %= size
    if n:
        parts.append(_under_thousand(n))
    return " ".join(parts)


_AMOUNT_RE = re.compile(r"(?:(?:₹|rs\.?|inr)\s?([\d,]+)(?:\s?rupees)?)|(?:([\d,]+)\s?rupees)", re.I)


def speakable(text: str) -> str:
    """Rupee amounts as words for TTS ("4,820 rupees" -> "four thousand eight
    hundred twenty rupees"), never "Rs." or "INR". Applied after the
    guardrail, which checks the digits."""
    def sub(m):
        digits = (m.group(1) or m.group(2)).replace(",", "")
        return f"{indian_words(int(digits))} rupees" if digits.isdigit() else m.group(0)
    return _AMOUNT_RE.sub(sub, text)


def spoken_digits(digits: str) -> str:
    """"4271" -> "four, two, seven, one": card digits are said one by one."""
    return ", ".join(_ONES[int(d)] for d in digits)


def _spoken_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{d.day}{suffix} {d.strftime('%B')}"


# --- per-customer lines --------------------------------------------------

def addressed(customer: dict) -> str:
    """"Mr. Rahul": salutation from the fixture, never guessed from the name."""
    return f"{customer['salutation']} {customer['customer_name']}" if customer.get("salutation") else customer["customer_name"]


def _pronoun(customer: dict) -> str:
    return {"Mr.": "him", "Ms.": "her", "Mrs.": "her"}.get(customer.get("salutation", ""), "them")


def _part_of_day(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    return "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"


def opening_line(customer: dict, now: datetime | None = None) -> str:
    """The first thing said on every call (voice-guide.md Stage 1). Spoken
    straight to TTS by bot/main.py rather than generated, so the customer
    isn't left in ~1.5s of silence while the LLM writes a fixed line."""
    return (f"Hi, good {_part_of_day(now)}! This is {AGENT_NAME} calling from {BANK_NAME}. "
            f"Am I speaking with {addressed(customer)}?")


def identity_question(customer: dict) -> str:
    return f"Am I speaking with {addressed(customer)}?"


def identity_reask(customer: dict) -> str:
    return f"Sorry, I didn't catch that. {identity_question(customer)}"


def card_question(customer: dict) -> str:
    return f"Is your {BANK_NAME} card the one ending in {spoken_digits(customer['card_last4'])}?"


def card_check_line(customer: dict) -> str:
    """Said once the name is confirmed: the AI and recording disclosure
    (voice-guide.md Stage 2, §10), then the second identity check."""
    return f"Thank you. I'm an automated assistant, and this call is recorded. {card_question(customer)}"


def card_reask(customer: dict) -> str:
    return f"Sorry, I didn't catch that. {card_question(customer)}"


def amount_details(customer: dict) -> str:
    """The scripted line spoken when the customer asks how much is due. Built
    in code so every figure is the fixture's. No total outstanding: that is
    only said after the OTP (brief), by bot/turn_gate.py."""
    if customer["late_fee"]:
        includes = f"That includes interest, and a late fee of {customer['late_fee']:,} rupees."
    else:
        includes = "That includes interest."
    return f"Your minimum amount due is {customer['min_due']:,} rupees. {includes}"


def total_line(customer: dict) -> str:
    return (f"Thank you, that's verified. Your total outstanding is {customer['total_outstanding']:,} rupees. "
            "When would suit you to make the payment?")


def third_party_line(customer: dict) -> str:
    return f"No problem. Is there a better time I could reach {_pronoun(customer)}?"


def build_system_prompt(customer: dict | None) -> str:
    """Fills the placeholders in bot/prompts/v1.py."""
    filled = (PROMPT.replace("<agent_name>", AGENT_NAME).replace("<bank_name>", BANK_NAME)
              .replace("<helpline>", HELPLINE).replace("<crisis_line>", CRISIS_LINE))
    if customer is None:
        return filled
    return (
        filled.replace("<opening_line>", opening_line(customer))
        .replace("<third_party_line>", third_party_line(customer))
        .replace("<identity_question>", identity_question(customer))
        .replace("<customer_name>", addressed(customer))
        .replace("<due_date>", _spoken_date(customer["due_date"]))
        .replace("<amount_details>", amount_details(customer))
    )


def build_developer_message(customer: dict | None) -> str | None:
    """The account facts the model may use. The total outstanding is left
    out on purpose: the model can't reveal what it was never told, and the
    total is only said by code after the OTP (Execution Plan §17)."""
    if customer is None:
        return None
    return (
        f"Today's date is {date.today().isoformat()}. "
        f"Due date: {customer['due_date']}. Minimum amount due: {customer['min_due']} rupees. "
        f"Interest charged this cycle: {customer['interest_charged']} rupees. "
        f"Late fee: {customer['late_fee']} rupees. "
        "Share these only after the customer has confirmed their identity, and only when they ask. "
        "You don't know the total outstanding; if they ask for it, the call system handles it."
    )


# --- fixed lines (Execution Plan §16-17) ---------------------------------
# Spoken by code, not generated, wherever the trigger can be detected
# deterministically. The prompt quotes the same strings for the cases only
# the model can recognise, and bot/guardrails.py lets them through, so a
# scripted line never trips the rule it exists to satisfy.

UNCLEAR_REASKS = [
    "Sorry, I didn't catch that clearly. Could you say that again?",
    "Sorry, the line broke up a little. Could you repeat that?",
    "I missed that, sorry. Could you say it once more?",
]
UNCLEAR_REASK = UNCLEAR_REASKS[0]
AUDIO_GLITCH = (
    "I'm having trouble hearing you clearly. It could be a glitch in my system. "
    "I'll arrange for a human agent to call you back."
)
IDENTITY_GLITCH = (
    "Sorry, I'm not able to verify your identity right now. It could be a glitch in my system. "
    "I'll arrange for a human agent to call you back."
)
WRONG_NUMBER_LINE = "I'm so sorry, I think I've got the wrong number. Have a good day."
# The purpose, stated gently (voice-guide.md Stage 3): no blame, no amount,
# an open question. Varied, since nothing else in the call repeats it.
PURPOSE_LINES = [
    "Great, thanks. It looks like this month's payment hasn't come through yet. Is everything okay?",
    "Thanks for confirming. It looks like this month's payment is still pending. Is everything alright?",
    "Got it, thanks. It looks like this month's payment hasn't come through yet. Is all well?",
]
OTP_ASK = (
    "Sure, I can share that. I've just sent a one-time code to your registered mobile. "
    "Could you read it out for me?"
)
WAIVER_LINE = (
    "Okay, a waiver request. I'm not allowed or trained to handle those, so I'll have a human "
    "support agent call you back."
)
DNC_LINE = "Understood, and I'm sorry for the calls. I've made a note, and we'll only follow up after a week. Take care."
CLOSING_LINE = "Thanks for your time, and have a good day."
CLOSING_LINE_FULL = "Thank you for your time. Have a good day."  # the prompt's close
PRE_IDENTITY_HELP = "Of course, I can help with that. Please confirm your identity so I can proceed."

# Answers to the caller-verification questions asked before identity is
# confirmed (D12). They reveal nothing about why we're calling: not the card,
# not the debt (D5; voice-guide.md Stage 1, "without revealing why").
PRE_IDENTITY_ANSWERS = {
    "name": f"I'm {AGENT_NAME}, from {BANK_NAME}.",
    "where": f"I'm calling from {BANK_NAME}.",
    "about": f"It's about your {BANK_NAME} account. I just need to confirm who I'm speaking to first.",
    "number": f"It's the number registered with {BANK_NAME}.",
    "scam": f"No, I'm a trained agent for {BANK_NAME}. I'll never ask for your card number, CVV or PIN.",
    "bot": (
        f"I'm an automated assistant from {BANK_NAME}. I can help with most things, but if you'd "
        "prefer a person, I'm happy to arrange that."
    ),
    "help": PRE_IDENTITY_HELP,
}

GLITCH_OUTCOMES = {
    "glitch_identity": {"type": "callback_request", "requested_time": "identity not verified - possible system glitch"},
    "glitch_audio": {"type": "callback_request", "requested_time": "audio unclear - possible system glitch"},
}


def scripted_lines(customer: dict | None) -> list[str]:
    """Every line the bot may say verbatim. bot/guardrails.py removes these
    from a reply before checking it, so only the model's own words are judged."""
    lines = [*UNCLEAR_REASKS, AUDIO_GLITCH, IDENTITY_GLITCH, WRONG_NUMBER_LINE, *PURPOSE_LINES, OTP_ASK,
             WAIVER_LINE, DNC_LINE, CLOSING_LINE, CLOSING_LINE_FULL, *PRE_IDENTITY_ANSWERS.values()]
    if customer is not None:
        lines += [identity_reask(customer), identity_question(customer), card_check_line(customer),
                  card_reask(customer), card_question(customer), third_party_line(customer),
                  amount_details(customer)]
    return lines
