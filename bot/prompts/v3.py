"""v2's prompt text, unchanged. The v2 -> v3 delta is code, not wording: outcome
validation, L4 guardrails, and the state machine are active around this same
prompt (see Execution.md §6, §7, §8). Kept as a separate module so the
v1/v2/v3 table in the writeup is reproducible straight from the repo.
"""

from bot.prompts.v2 import PROMPT

__all__ = ["PROMPT"]
