"""Decides, in code, what happens to each final customer transcription
before the LLM sees it (Execution Plan §16.3-16.5).

Most turns pass straight through to the LLM. Three kinds don't:
- unclear STT output (bot/stt_filter.py): the bot asks the customer to repeat,
  and after two unclear turns in a row says the glitch line and files a
  callback (D11, D12);
- a waiver request before identity: "please confirm your identity" (D12);
- a waiver request after identity: the fixed waiver line, the closing line,
  a callback_request, and the call ends (D13).

The trigger for each is deterministic, so the reply is a fixed line rather
than something the model might paraphrase into a guardrail hit. Shared by
bot/main.py and the replay evals (evals/live_cases.py), so a replayed call
goes through exactly the gate the live call did.
"""

import re
from dataclasses import dataclass

from bot import prompting
from bot.state import CallState
from bot.stt_filter import unclear_reason

WAIVER_REQUEST_RE = re.compile(
    r"\b(waive|waiver|waived|discount|settle|settlement|reduce|reduction|concession|maaf)\b", re.I
)


@dataclass
class GateResult:
    kind: str  # reask_identity | reask | glitch_identity | glitch_audio | pre_identity_help | waiver
    line: str  # spoken straight to TTS
    unclear_reason: str | None = None
    outcome: dict | None = None  # recorded by code when set
    end_call: bool = False


def gate_turn(state: CallState, text: str, customer: dict, *, confidence: float | None = None,
              config: dict | None = None) -> GateResult | None:
    """None means "pass the turn to the LLM". Updates `state` either way."""
    reason = unclear_reason(text, confidence=confidence, config=config)
    if reason:
        kind = state.observe_unclear_turn()
        state.signals.add("stt_unclear")
        if kind == "reask_identity":
            return GateResult(kind, prompting.identity_reask(customer), reason)
        if kind == "reask":
            line = prompting.UNCLEAR_REASKS[(state.unclear_turns - 1) % len(prompting.UNCLEAR_REASKS)]
            return GateResult(kind, line, reason)
        line = prompting.IDENTITY_GLITCH if kind == "glitch_identity" else prompting.AUDIO_GLITCH
        return GateResult(kind, line, reason, outcome=dict(prompting.GLITCH_OUTCOMES[kind]), end_call=True)

    state.note_turn()
    state.observe_customer_turn(text, expected_otp=customer.get("otp"))

    if WAIVER_REQUEST_RE.search(text):
        if state.identity_confirmed:
            state.signals.add("waiver_requested")
            return GateResult(
                "waiver",
                f"{prompting.WAIVER_LINE} {prompting.CLOSING_LINE}",
                outcome={"type": "callback_request", "requested_time": f"waiver request - customer said '{text}'"},
                end_call=True,
            )
        if not state.identity_denied:
            return GateResult("pre_identity_help",
                              f"{prompting.PRE_IDENTITY_HELP} {prompting.identity_question(customer)}")
    return None
