"""L3 state machine (Execution.md §8). Deterministic, not prompt-trusted:
tracks identity/OTP/de-escalation/hardship/turn-budget state across a call
so bot/guardrails.py and the outcome handler can gate on established fact,
not on what the prompt merely asked the model to do.

Detection here is keyword/regex heuristics, not full NLU — a documented
scope cut (see writeup "what I'd cut"), chosen because real identity/abuse/
hardship classification would need either a second LLM call per customer
turn (cost + latency on every turn) or a labelled dataset to train a
classifier, neither of which fits a 3-day take-home. The heuristics are
intentionally conservative: ambiguous input leaves the guardrail armed
(e.g. identity stays unconfirmed) rather than guessing permissive.

This module has no Pipecat dependency, so the exact same code drives the
live pipeline (bot/main.py) and the eval harness (evals/harness.py) — the
state machine itself isn't a thing either one reimplements.
"""

import re
from dataclasses import dataclass, field

# Turn budget is expressed in turns, not wall-clock seconds, because the
# eval harness's text turns don't take real speech-paced time (each is just
# LLM round-trip latency) — there is no faithful "elapsed minutes" signal to
# share between the harness and a live call. Thresholds assume ~20s per
# bot+customer exchange on a real voice call, which is the same assumption
# Execution.md §8 states in minutes.
SOFT_NUDGE_TURNS = 8  # ~2.5 min
HARD_STOP_TURNS = 12  # ~4 min

_AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|haan|ha ha|speaking|that'?s me|this is (?:him|her|me))\b", re.I
)
_DENY_RE = re.compile(
    # "Who's this?" is not a denial: under D12 it gets a scripted answer and
    # the identity question again (Execution Plan §16.4).
    r"\b(no|nahi|wrong number|not (?:him|her|me)|he'?s not here|she'?s not here)\b",
    re.I,
)
_HARDSHIP_RE = re.compile(
    r"\b(hospital|hospitalized|hospitalised|surgery|\bicu\b|passed away|died|death in the family|"
    r"lost my job|laid off|unemployed|job loss)\b",
    re.I,
)
_THREAT_RE = re.compile(
    r"\b(kill you|hurt you|beat you|come (?:to|over) (?:your|the) (?:office|house))\b", re.I
)
_SLUR_RE = re.compile(r"\b(bastard|chutiya|madarchod|bhenchod|saala)\b", re.I)
_LEGAL_MENTION_RE = re.compile(r"\b(lawyer|court|ombudsman|police|legal action)\b", re.I)
_ABUSE_RE = re.compile(r"\b(idiot|useless|stupid|shut up|scam|fraud|harass(?:ing)?)\b", re.I)
_OTP_DIGITS_RE = re.compile(r"\b\d{6}\b")


def identity_answer(text: str) -> str | None:
    """'affirm', 'deny' or None for an answer to "Am I speaking to X?".
    When both appear, the last one wins: "No, I said yes" is a yes (seen live
    on 2026-10-28, when a deny-first check turned a real yes into a denial)."""
    last = None
    for kind, regex in (("affirm", _AFFIRM_RE), ("deny", _DENY_RE)):
        for m in regex.finditer(text):
            if last is None or m.start() > last[1]:
                last = (kind, m.start())
    return last[0] if last else None


@dataclass
class CallState:
    """Mutable per-call state. One instance per call, threaded through both
    the harness's text loop and the live pipeline's event handlers."""

    identity_confirmed: bool = False
    identity_denied: bool = False
    otp_verified: bool = False
    otp_attempted: bool = False
    hardship_disclosed: bool = False
    threat_detected: bool = False
    abuse_instances: int = 0
    deescalation_used: bool = False
    turn_count: int = 0
    unclear_streak: int = 0  # consecutive turns the STT filter marked unclear
    unclear_turns: int = 0
    signals: set[str] = field(default_factory=set)
    _nudged_soft: bool = field(default=False, repr=False)
    _nudged_hard: bool = field(default=False, repr=False)
    _nudged_abuse: bool = field(default=False, repr=False)

    def note_turn(self) -> None:
        self.turn_count += 1

    def pending_nudges(self) -> list[str]:
        """One-shot developer-message nudges for whichever thresholds this
        call has newly crossed. Kept on the instance (not module state) so
        concurrent calls in the eval harness never share nudge flags."""
        nudges = []
        status = self.turn_budget_status
        if status == "soft_nudge" and not self._nudged_soft:
            self._nudged_soft = True
            nudges.append(
                "You are approaching the target call length. Start moving toward closing the "
                "call with a recorded outcome."
            )
        if status == "hard_stop" and not self._nudged_hard:
            self._nudged_hard = True
            nudges.append(
                "You are well past the target call length. End the call now: record whatever "
                "outcome is true (a callback_request or no_outcome if nothing concrete was "
                "agreed) rather than continuing."
            )
        if self.must_transfer_for_abuse and not self._nudged_abuse:
            self._nudged_abuse = True
            nudges.append(
                "The customer has been abusive more than once. Do not attempt to de-escalate "
                "again — record a dispute_transfer now."
            )
        return nudges

    @property
    def turn_budget_status(self) -> str:
        """'ok' | 'soft_nudge' | 'hard_stop'. Suppressed entirely once
        hardship is disclosed — a distressed customer is never cut off by a
        clock (Execution.md §8's stated hard-to-get-wrong case)."""
        if self.hardship_disclosed:
            return "ok"
        if self.turn_count >= HARD_STOP_TURNS:
            return "hard_stop"
        if self.turn_count >= SOFT_NUDGE_TURNS:
            return "soft_nudge"
        return "ok"

    @property
    def must_transfer_for_abuse(self) -> bool:
        """D4: one de-escalation attempt, then a second abuse instance
        forces a transfer rather than another attempt."""
        return self.abuse_instances >= 2

    def observe_customer_turn(self, text: str, *, expected_otp: str | None) -> None:
        """Update state from the customer's latest utterance."""
        self.unclear_streak = 0
        if not self.identity_confirmed and not self.identity_denied:
            answer = identity_answer(text)
            if answer == "deny":
                self.identity_denied = True
                self.signals.add("identity_unverified")
            elif answer == "affirm":
                self.identity_confirmed = True

        if _HARDSHIP_RE.search(text):
            self.hardship_disclosed = True
            self.signals.add("hardship_disclosed")

        if _THREAT_RE.search(text) or _SLUR_RE.search(text):
            self.threat_detected = True
            self.signals.add("abuse_detected")
        elif _LEGAL_MENTION_RE.search(text):
            self.signals.add("legal_threat")
        elif _ABUSE_RE.search(text):
            self.abuse_instances += 1
            self.signals.add("abuse_detected")

        if expected_otp is not None and not self.otp_verified:
            digits = _OTP_DIGITS_RE.search(text)
            if digits:
                self.otp_attempted = True
                if digits.group() == expected_otp:
                    self.otp_verified = True
                else:
                    self.signals.add("otp_failed")

    def observe_unclear_turn(self) -> str:
        """A turn the STT filter (bot/stt_filter.py) marked unclear. Returns
        what the bot does instead of answering it (Execution Plan §16.3-16.4):
        'reask_identity' / 'reask' the first time, 'glitch_identity' /
        'glitch_audio' on the second unclear turn in a row."""
        self.unclear_streak += 1
        self.unclear_turns += 1
        before_identity = not self.identity_confirmed and not self.identity_denied
        if self.unclear_streak >= 2:
            return "glitch_identity" if before_identity else "glitch_audio"
        return "reask_identity" if before_identity else "reask"

    def note_deescalation_attempt(self) -> None:
        self.deescalation_used = True
