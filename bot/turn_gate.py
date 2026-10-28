"""Decides, in code, what happens to each final customer transcription
before the LLM sees it (Execution Plan §16-17).

Most turns pass straight through to the LLM. These don't:
- unclear STT output (bot/stt_filter.py): the bot asks the customer to repeat,
  and after two unclear turns in a row says the glitch line and files a
  callback (D11, D12);
- the identity steps: name confirmed -> AI and recording disclosure plus
  the card check; card confirmed -> the purpose line; card denied -> the
  wrong-number line, and the call ends (voice-guide.md Stages 1-3);
- a request for the total outstanding: the OTP ask, then the total once a
  code is read out (the brief: no balance without OTP);
- "stop calling me": noted, a follow-up in 7 days, and the call ends;
- a waiver request: before identity, "please confirm your identity"; after
  it, the fixed waiver line, a callback_request, and the call ends (D13).

The trigger for each is deterministic, so the reply is a fixed line rather
than something the model might paraphrase into a guardrail hit. Shared by
bot/main.py, the persona harness and the replay evals, so every eval goes
through exactly the gate the live call does.
"""

import re
from dataclasses import dataclass

from bot import prompting
from bot.state import CallState
from bot.stt_filter import unclear_reason

WAIVER_REQUEST_RE = re.compile(
    r"\b(waive|waiver|waived|discount|settle|settlement|reduce|reduction|concession|maaf)\b"
    r"|\b(cancel|remove|drop|reverse)\b[^.?!]*\b(fee|fees|charge|charges|interest)\b", re.I
)
DNC_RE = re.compile(
    r"\b(stop calling|don'?t call me|do not call me|never call me|don'?t call (?:me )?again|"
    r"call mat karo|phone mat karo)\b", re.I
)
# A question about the total, not a promise to pay it ("I'll pay the full amount").
TOTAL_REQUEST_RE = re.compile(
    r"\b(what(?:'s| is)|how much|tell me|know|check|kitna)\b.*\b(total|outstanding|balance|owe|full amount|baaki)\b"
    r"|\b(total|outstanding|balance)\b[^.]*\?", re.I
)


@dataclass
class GateResult:
    kind: str
    line: str  # spoken straight to TTS
    unclear_reason: str | None = None
    outcome: dict | None = None  # recorded by code when set
    end_call: bool = False


def _rotate(lines: list[str], n: int) -> str:
    return lines[n % len(lines)]


def gate_turn(state: CallState, text: str, customer: dict, *, confidence: float | None = None,
              config: dict | None = None) -> GateResult | None:
    """None means "pass the turn to the LLM". Updates `state` either way."""
    reason = unclear_reason(text, confidence=confidence, config=config)
    if reason:
        kind = state.observe_unclear_turn()
        state.signals.add("stt_unclear")
        if kind == "reask_identity":
            line = prompting.card_reask(customer) if state.name_confirmed else prompting.identity_reask(customer)
            return GateResult(kind, line, reason)
        if kind == "reask":
            return GateResult(kind, _rotate(prompting.UNCLEAR_REASKS, state.unclear_turns - 1), reason)
        line = prompting.IDENTITY_GLITCH if kind == "glitch_identity" else prompting.AUDIO_GLITCH
        return GateResult(kind, line, reason, outcome=dict(prompting.GLITCH_OUTCOMES[kind]), end_call=True)

    name_before, identity_before = state.name_confirmed, state.identity_confirmed
    state.note_turn()
    state.observe_customer_turn(text, expected_otp=customer.get("otp"))

    if state.wrong_number:
        return GateResult("wrong_number", prompting.WRONG_NUMBER_LINE,
                          outcome={"type": "no_outcome", "reason": "wrong number - card not confirmed"},
                          end_call=True)

    if DNC_RE.search(text):
        state.signals.add("do_not_call")
        return GateResult("do_not_call", prompting.DNC_LINE,
                          outcome={"type": "callback_request",
                                   "requested_time": "do-not-call request - follow up in 7 days"},
                          end_call=True)

    if WAIVER_REQUEST_RE.search(text):
        if state.identity_confirmed:
            state.signals.add("waiver_requested")
            return GateResult(
                "waiver",
                f"{prompting.WAIVER_LINE} {prompting.CLOSING_LINE}",
                outcome={"type": "callback_request", "requested_time": f"waiver request - customer said '{text}'"},
                end_call=True,
            )

    # The disclosure and card check come first, even if the customer also
    # asked something in the same breath.
    if state.name_confirmed and not name_before and not state.identity_confirmed:
        return GateResult("card_check", prompting.card_check_line(customer))
    if state.identity_confirmed and not identity_before:
        return GateResult("purpose", _rotate(prompting.PURPOSE_LINES, state.turn_count))

    if not state.identity_confirmed and not state.identity_denied and (
            WAIVER_REQUEST_RE.search(text) or TOTAL_REQUEST_RE.search(text)):
        question = prompting.card_question(customer) if state.name_confirmed else prompting.identity_question(customer)
        return GateResult("pre_identity_help", f"{prompting.PRE_IDENTITY_HELP} {question}")

    if state.otp_verified and not state.total_shared:
        state.total_shared = True
        return GateResult("total", prompting.total_line(customer))
    if state.identity_confirmed and not state.otp_requested and TOTAL_REQUEST_RE.search(text):
        state.otp_requested = True
        return GateResult("otp_ask", prompting.OTP_ASK)
    return None
