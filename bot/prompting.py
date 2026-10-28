"""Shared system-prompt / developer-fact construction.

Both the live pipeline (bot/main.py, over Pipecat/WebRTC) and the eval
harness (evals/harness.py, text-only against the OpenAI API directly) build
the prompt from the same two functions here, so an eval result is a result
about the shipped prompt, not a copy of it. What differs between the two
callers is the transport only.
"""

from bot.prompts import v1, v2, v3

PROMPTS = {"v1": v1.PROMPT, "v2": v2.PROMPT, "v3": v3.PROMPT}


def build_system_prompt(version: str, customer: dict | None) -> str:
    """v1 is used exactly as given — no customer facts injected, since the
    brief's prompt assumes the model 'knows everything' without being told
    anything. v2/v3 fill in the <customer_name> placeholder."""
    prompt = PROMPTS[version]
    if version == "v1" or customer is None:
        return prompt
    return prompt.replace("<customer_name>", customer["customer_name"])


def build_developer_message(version: str, customer: dict | None) -> str | None:
    if version == "v1" or customer is None:
        return None
    return (
        f"Minimum amount due: Rs {customer['min_due']}. Due date: {customer['due_date']}. "
        "Do not state the total outstanding balance unless the customer has passed OTP "
        "verification in this call."
    )
