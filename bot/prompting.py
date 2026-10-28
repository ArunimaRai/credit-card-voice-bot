"""Shared system-prompt / developer-fact construction.

Both the live pipeline (bot/main.py, over Pipecat/WebRTC) and the eval
harness (evals/harness.py, text-only against the OpenAI API directly) build
the prompt from the same two functions here, so an eval result is a result
about the shipped prompt, not a copy of it. What differs between the two
callers is the transport only.
"""

from datetime import date

from bot.prompts.v1 import PROMPT

# Recorded with every call and eval row, so results stay attributable if a
# v2 ever exists.
PROMPT_VERSION = "v1"


def _spoken_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{d.day}{suffix} {d.strftime('%B')}"


def account_summary(customer: dict) -> str:
    """The scripted line spoken right after identity is confirmed. Built in
    code, not left to the model, so every figure in it is the fixture's."""
    if customer["late_fee"]:
        includes = f"That includes interest, and a late payment fee of {customer['late_fee']:,} rupees."
    else:
        includes = "That includes interest."
    return (
        "Great, thanks. "
        f"I wanted to remind you that your credit card payment was due on {_spoken_date(customer['due_date'])}. "
        f"Your minimum amount due is {customer['min_due']:,} rupees. "
        f"{includes} "
        f"Your total outstanding bill is {customer['total_outstanding']:,} rupees."
    )


def build_system_prompt(customer: dict | None) -> str:
    """Fills the <customer_name> and <account_summary> placeholders."""
    if customer is None:
        return PROMPT
    return PROMPT.replace("<customer_name>", customer["customer_name"]).replace(
        "<account_summary>", account_summary(customer)
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
        "Share these only after the customer has confirmed their identity."
    )
