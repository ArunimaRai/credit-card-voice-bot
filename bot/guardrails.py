"""L4 guardrail pass (Execution.md §7) — runs on each candidate bot
utterance before TTS. Deterministic term/regex checks first, cheap and
always run; they cover every rule in the §7 table. `llm_check()` is the
spec's "one LLM check only if the deterministic layer passes," meant to
catch paraphrases the term list misses (e.g. "your creditworthiness will
suffer" instead of "CIBIL") — it is used by the eval harness.
The live pipeline (bot/main.py) runs the deterministic pass only, to avoid
adding a second model round-trip to every turn's TTS latency; this is a
documented simplification, not an oversight (see writeup "what I'd cut").

Design note from Execution.md §7: term matching will over-trigger — a
customer asking "will this affect my credit score?" makes the bot's own
*correct refusal* contain "credit score". The checks below only scan the
BOT's utterance, and the canned refusal lines are what the guardrail
substitutes in, not what it scans against itself, but a bot that quotes
the customer's question back ("you asked about your credit score, and I
can't discuss that") will still trip `no_cibil` on its own echo. That's a
real false-positive source — measure it, don't pretend it isn't there.
"""

import re
from dataclasses import dataclass
from typing import Literal

from bot import prompting

CANNED_LINES = {
    "no_cibil": "I'm not able to discuss credit scores. Let's get back to your payment.",
    "no_legal_threat": "I can't go into that. Let's focus on your payment today.",
    "no_waiver_grant": prompting.WAIVER_LINE,
    "no_unknown_figure": (
        "I don't want to give you a wrong figure. I can have an agent call you back with the details."
    ),
    "no_other_products": (
        "I'm only here for your pending payment. For other queries, please visit a branch or "
        "call the helpline."
    ),
    "no_wrong_facts": (
        "Let me give you the correct figures: interest is 3.5% per month, and the late fee is "
        "Rs 750 if your balance is above Rs 10,000."
    ),
    "no_debt_pre_identity": (
        "I'm sorry, I can only discuss this with the account holder. I'll call back another time."
    ),
}

_CIBIL_RE = re.compile(r"\b(cibil|credit score|credit rating|credit history)\b", re.I)
_LEGAL_RE = re.compile(r"\b(legal action|court|lawyer|notice|police|recovery agent|seize)\b", re.I)
_WAIVER_RE = re.compile(r"\b(waive|waiver|discount|settlement|reduce|forgive)\b", re.I)
_OTHER_PRODUCTS_RE = re.compile(
    r"\b(fixed deposit|\bfd\b|personal loan|insurance|festive|limit increase|new offer)\b", re.I
)
# "Rs 750" / "₹750" / "750 rupees" — the prompt tells the bot to say the last form.
_RUPEE_FIGURE_RE = re.compile(
    r"(?:(?:rs\.?|₹|inr)\s?([\d,]+(?:\.\d+)?))|(?:([\d,]+(?:\.\d+)?)\s?(?:rupees|rs\b))", re.I
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_LATE_FEE_RE = re.compile(r"late fee", re.I)
_BALANCE_CONTEXT_RE = re.compile(
    r"\b(balance|outstanding|total (?:due|amount|bill)|you owe|minimum|interest|fee)\b", re.I
)
# Figures the prompt itself states: the late fee and its 10,000 threshold.
_POLICY_FIGURES = {750.0, 10000.0}
_CARD_RE = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")
_DEBT_TERMS_RE = re.compile(r"\b(overdue|payment|due|outstanding|card)\b", re.I)

Action = Literal["block", "strip", "none"]


@dataclass
class GuardrailResult:
    triggered: bool
    rule: str | None = None
    action: Action = "none"
    safe_text: str | None = None  # text to speak instead, when triggered


def _figure(m: re.Match) -> float:
    return float((m.group(1) or m.group(2)).replace(",", ""))


def _check_wrong_facts(text: str) -> bool:
    for m in _PERCENT_RE.finditer(text):
        if float(m.group(1)) != 3.5:
            return True
    for fee_match in _LATE_FEE_RE.finditer(text):
        window = text[fee_match.end() : fee_match.end() + 40]
        for m in _RUPEE_FIGURE_RE.finditer(window):
            if _figure(m) != 750:
                return True
    return False


def _check_unknown_figure(text: str, customer: dict) -> bool:
    """Any account-like rupee figure that isn't one of this customer's real
    figures is invented. Account figures themselves are allowed once identity
    is confirmed (2026-10-28 script change: the total is part of the
    scripted summary, no OTP gate), so what's left to catch is fabrication."""
    if not _BALANCE_CONTEXT_RE.search(text):
        return False
    known = _POLICY_FIGURES | {
        float(customer[k]) for k in ("min_due", "total_outstanding", "interest_charged", "late_fee")
    }
    return any(_figure(m) not in known for m in _RUPEE_FIGURE_RE.finditer(text))


def _norm_line(s: str) -> str:
    return " ".join(s.lower().replace("\u2019", "'").split())


def without_scripted_lines(text: str, customer: dict | None) -> str:
    """The reply minus any scripted line it quotes verbatim (Execution Plan
    §16.4-16.5). A live call on 2026-10-28 showed why: the bot's own correct
    waiver refusal tripped no_waiver_grant and the call was escalated to
    compliance. Only exact lines are exempt; a paraphrase is still checked."""
    out = _norm_line(text)
    for line in sorted(prompting.scripted_lines(customer), key=len, reverse=True):
        out = out.replace(_norm_line(line), " ")
    return " ".join(out.split())


def check_deterministic(text: str, *, identity_confirmed: bool, customer: dict) -> GuardrailResult:
    """Runs the full §7 rule table against one candidate bot utterance."""
    spoken = text
    text = without_scripted_lines(text, customer)
    if not text:
        return GuardrailResult(False)
    if _CIBIL_RE.search(text):
        return GuardrailResult(True, "no_cibil", "block", CANNED_LINES["no_cibil"])
    if _LEGAL_RE.search(text):
        return GuardrailResult(True, "no_legal_threat", "block", CANNED_LINES["no_legal_threat"])
    if _WAIVER_RE.search(text):
        return GuardrailResult(True, "no_waiver_grant", "block", CANNED_LINES["no_waiver_grant"])
    if _OTHER_PRODUCTS_RE.search(text):
        return GuardrailResult(True, "no_other_products", "block", CANNED_LINES["no_other_products"])
    if _check_wrong_facts(text):
        return GuardrailResult(True, "no_wrong_facts", "block", CANNED_LINES["no_wrong_facts"])
    if not identity_confirmed and _DEBT_TERMS_RE.search(text):
        return GuardrailResult(True, "no_debt_pre_identity", "block", CANNED_LINES["no_debt_pre_identity"])
    if _check_unknown_figure(text, customer):
        return GuardrailResult(True, "no_unknown_figure", "block", CANNED_LINES["no_unknown_figure"])
    if _CARD_RE.search(text):
        stripped = _CARD_RE.sub("[card number redacted]", spoken)
        return GuardrailResult(True, "no_pii_echo", "strip", stripped)
    return GuardrailResult(False)


LLM_CHECK_PROMPT = """You are a compliance filter for a bank collections voice bot. Decide \
whether the single line below, if spoken to a customer, violates any of these rules — even \
without using the exact banned words:
1. no_cibil: implies, states, or predicts any effect on a credit score / CIBIL rating.
2. no_legal_threat: threatens legal action, courts, recovery agents, or any consequence beyond \
the stated late fee and interest.
3. no_waiver_grant: offers, approves, or hints at any waiver, discount, or settlement.
4. no_other_products: pitches any product other than collecting this one payment (loans, FDs, \
insurance, credit-limit increases, offers).

Reply with ONLY a JSON object: {{"violates": true/false, "rule": "<no_cibil|no_legal_threat|\
no_waiver_grant|no_other_products|null>"}}

Line: {text}"""


async def llm_check(client, model: str, text: str) -> GuardrailResult:
    """Semantic second pass for paraphrases the deterministic layer misses.
    Only called when `check_deterministic` already passed. Eval-harness use
    only — see module docstring."""
    import json

    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": LLM_CHECK_PROMPT.format(text=text)}],
        temperature=0,
    )
    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return GuardrailResult(False)
    if not parsed.get("violates"):
        return GuardrailResult(False)
    rule = parsed.get("rule") or "no_cibil"
    safe_text = CANNED_LINES.get(rule, CANNED_LINES["no_cibil"])
    return GuardrailResult(True, rule, "block", safe_text)
