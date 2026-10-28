"""Live-call fixes (Execution Plan §16): the STT junk filter, the identity
flow, the turn gate, the guardrail exceptions for scripted lines, waiver
routing and the new agent checks. Cases come from real calls where they can.

Run with: uv run python -m unittest discover tests
"""

import json
import unittest
from datetime import date
from pathlib import Path

from bot import guardrails, prompting
from bot.analysis import derive_workflow, merge_unclear_turns, review_agent
from bot.state import CallState, identity_answer
from bot.stt_filter import DEFAULT_CONFIG, unclear_reason
from bot.turn_gate import gate_turn

CUSTOMER = json.loads((Path(__file__).resolve().parent.parent / "bot" / "fixtures" / "customers.json").read_text())["MB-0007"]
TODAY = date(2026, 10, 28)


def reason(text, confidence=None):
    return unclear_reason(text, confidence=confidence, config=DEFAULT_CONFIG)


class CustomerNameTest(unittest.TestCase):
    def test_customer_is_not_the_agent(self):
        self.assertEqual(CUSTOMER["customer_name"], "Rahul")
        self.assertEqual(prompting.opening_line(CUSTOMER),
                         "Hello, this is Shreya calling from Mumbai Bank. Am I speaking to Rahul?")


class SttFilterTest(unittest.TestCase):
    def test_junk_seen_on_live_calls(self):
        self.assertEqual(reason("context:"), "hallucination_marker")
        self.assertEqual(reason("context:###"), "hallucination_marker")
        self.assertEqual(reason("minimum_due"), "hallucination_marker")
        self.assertEqual(reason("This is a phone call in English between a Mumbai Bank collections agent"),
                         "hallucination_marker")
        self.assertEqual(reason("그렇지."), "foreign_script")
        self.assertEqual(reason("عليك"), "foreign_script")
        self.assertEqual(reason("waiver."), "lone_word")
        self.assertEqual(reason("..."), "empty")

    def test_clear_speech_passes(self):
        for text in ["Yes.", "No, I said yes.", "What is my payment amount?", "haan", "Okay, sure.", "Great."]:
            self.assertIsNone(reason(text), text)

    def test_hinglish_in_devanagari_passes(self):
        self.assertIsNone(reason("हाँ, मैं राहुल बोल रहा हूँ"))

    def test_confidence(self):
        self.assertEqual(reason("That's right,", confidence=0.035), "low_confidence")
        self.assertIsNone(reason("waiver", confidence=0.95))  # a confident one-word answer is fine
        self.assertEqual(reason("waiver", confidence=0.6), "lone_word")


class IdentityAnswerTest(unittest.TestCase):
    def test_last_answer_wins(self):
        self.assertEqual(identity_answer("No, I said yes."), "affirm")  # live call 2f9ae2f8
        self.assertEqual(identity_answer("no no, yes it is me"), "affirm")
        self.assertEqual(identity_answer("Yes... actually no, he's not here"), "deny")

    def test_questions_are_not_answers(self):
        for text in ["Who's this?", "Who is calling?", "What is this about?"]:
            self.assertIsNone(identity_answer(text), text)


class TurnGateTest(unittest.TestCase):
    def test_unclear_identity_then_glitch(self):
        s = CallState()
        g = gate_turn(s, "context:", CUSTOMER)
        self.assertEqual((g.kind, g.line), ("reask_identity", "Sorry, I didn't catch that. Am I speaking to Rahul?"))
        self.assertFalse(g.end_call)
        g = gate_turn(s, "Obrigado.", CUSTOMER, confidence=0.04)
        self.assertEqual(g.kind, "glitch_identity")
        self.assertEqual(g.line, prompting.IDENTITY_GLITCH)
        self.assertEqual(g.outcome, {"type": "callback_request",
                                     "requested_time": "identity not verified - possible system glitch"})
        self.assertTrue(g.end_call)

    def test_clear_turn_resets_the_streak(self):
        s = CallState()
        self.assertIsNone(gate_turn(s, "Yes, speaking.", CUSTOMER))
        self.assertEqual(gate_turn(s, "minimum_due", CUSTOMER).kind, "reask")
        self.assertIsNone(gate_turn(s, "What is my payment amount?", CUSTOMER))
        self.assertEqual(gate_turn(s, "context:", CUSTOMER).kind, "reask")
        g = gate_turn(s, "###", CUSTOMER)
        self.assertEqual((g.kind, g.line), ("glitch_audio", prompting.AUDIO_GLITCH))
        self.assertEqual(g.outcome["requested_time"], "audio unclear - possible system glitch")

    def test_repeated_reasks_vary(self):
        s = CallState()
        gate_turn(s, "Yes", CUSTOMER)
        first = gate_turn(s, "context:", CUSTOMER).line
        gate_turn(s, "Okay.", CUSTOMER)
        second = gate_turn(s, "###", CUSTOMER).line
        self.assertNotEqual(first, second)
        self.assertIn(second, prompting.UNCLEAR_REASKS)

    def test_scripted_turns_are_three_sentences_at_most(self):
        """voice-guide.md §13: every agent turn is three sentences or fewer."""
        import re
        turns = [*prompting.UNCLEAR_REASKS, prompting.AUDIO_GLITCH, prompting.IDENTITY_GLITCH,
                 f"{prompting.WAIVER_LINE} {prompting.CLOSING_LINE}",
                 *(f"{a} {prompting.identity_question(CUSTOMER)}" for a in prompting.PRE_IDENTITY_ANSWERS.values())]
        for t in turns:
            self.assertLessEqual(len(re.findall(r"[.?!](?:\s|$)", t)), 3, t)

    def test_pre_identity_answers_reveal_nothing(self):
        """voice-guide.md Stage 1: confirm the cardholder without revealing why."""
        import re
        for key, line in prompting.PRE_IDENTITY_ANSWERS.items():
            if key == "scam":  # names the card only to say what we'll never ask for
                continue
            self.assertIsNone(re.search(r"\b(card|payment|due|overdue|owe|amount)\b", line, re.I), line)

    def test_live_call_2f9ae2f8(self):
        """The call that escalated a made-up "waiver." to compliance."""
        s = CallState()
        self.assertEqual(gate_turn(s, "context:", CUSTOMER).kind, "reask_identity")
        self.assertIsNone(gate_turn(s, "No, I said yes.", CUSTOMER))
        self.assertTrue(s.identity_confirmed)
        self.assertFalse(s.identity_denied)
        g = gate_turn(s, "waiver.", CUSTOMER)
        self.assertEqual(g.kind, "reask")
        self.assertNotIn("waiver_requested", s.signals)

    def test_waiver_before_identity(self):
        s = CallState()
        g = gate_turn(s, "Can I get a waiver on the late fee?", CUSTOMER)
        self.assertEqual(g.kind, "pre_identity_help")
        self.assertEqual(g.line, f"{prompting.PRE_IDENTITY_HELP} Am I speaking to Rahul?")
        self.assertIsNone(g.outcome)

    def test_waiver_after_identity_ends_the_call(self):
        s = CallState()
        gate_turn(s, "Yes", CUSTOMER)
        g = gate_turn(s, "Can you waive the late fee?", CUSTOMER)
        self.assertEqual(g.kind, "waiver")
        self.assertEqual(g.line, f"{prompting.WAIVER_LINE} {prompting.CLOSING_LINE}")
        self.assertEqual(g.outcome["type"], "callback_request")
        self.assertTrue(g.outcome["requested_time"].startswith("waiver request"))
        self.assertTrue(g.end_call)
        self.assertIn("waiver_requested", s.signals)


class GuardrailExceptionsTest(unittest.TestCase):
    def check(self, text, confirmed=False):
        return guardrails.check_deterministic(text, identity_confirmed=confirmed, customer=CUSTOMER).rule

    def test_scripted_lines_pass(self):
        for line in prompting.PRE_IDENTITY_ANSWERS.values():
            self.assertIsNone(self.check(f"{line} Am I speaking to Rahul?"), line)
        self.assertIsNone(self.check(f"{prompting.WAIVER_LINE} {prompting.CLOSING_LINE}", confirmed=True))
        self.assertIsNone(self.check(prompting.IDENTITY_GLITCH))

    def test_violations_still_blocked(self):
        self.assertEqual(self.check("It's about your credit card payment that is due."), "no_debt_pre_identity")
        self.assertEqual(self.check("I can waive the late fee for you.", confirmed=True), "no_waiver_grant")
        # A scripted line doesn't excuse what comes after it.
        self.assertEqual(self.check(f"{prompting.WAIVER_LINE} I could give you a small discount though.", confirmed=True),
                         "no_waiver_grant")

    def test_waiver_canned_line_is_the_scripted_one(self):
        self.assertEqual(guardrails.CANNED_LINES["no_waiver_grant"], prompting.WAIVER_LINE)


class WaiverRoutingTest(unittest.TestCase):
    def test_waiver_is_a_callback_with_a_flag(self):
        w = derive_workflow({"type": "callback_request", "requested_time": "waiver request - customer said 'waive it'"},
                            ["waiver_requested"], identity_confirmed=True, identity_denied=False, today=TODAY)
        self.assertEqual(w["stage"]["key"], "callback_scheduled")
        self.assertIsNone(w["stage"]["overridden_from"])
        self.assertIn("waiver_requested", [f["key"] for f in w["flags"]])

    def test_glitch_callback(self):
        w = derive_workflow({"type": "callback_request", "requested_time": "identity not verified - possible system glitch"},
                            ["stt_unclear"], identity_confirmed=False, identity_denied=False, today=TODAY)
        self.assertEqual(w["stage"]["key"], "callback_scheduled")
        self.assertEqual(w["follow_up"]["date_source"], "default_sla")


class LiveChecksTest(unittest.TestCase):
    LIVE_2F9 = {
        "identity_confirmed": False, "identity_denied": True, "outcome": {"type": "callback_request"},
        "guardrail_triggers": [{"turn": 3, "rule": "no_waiver_grant", "action": "substituted"}],
        "transcript": [
            {"role": "bot", "text": "Hello, this is Shreya calling from Mumbai Bank. Am I speaking to Shreya?"},
            {"role": "customer", "text": "context:"},
            {"role": "bot", "text": "Okay, I'll call back later."},
            {"role": "customer", "text": "No, I said yes."},
            {"role": "customer", "text": "waiver."},
            {"role": "bot", "text": "I've noted that, and a human agent will call you back about it — I can't approve anything myself."},
        ],
    }

    def test_live_failure_is_caught(self):
        keys = {f["key"] for f in review_agent(self.LIVE_2F9)["findings"]}
        self.assertTrue({"acted_on_unclear", "identity_flow", "pre_identity_topic", "unanswered_turn"} <= keys)

    def test_gated_turns_are_not_findings(self):
        record = {
            "identity_confirmed": True, "identity_denied": False, "outcome": {"type": "callback_request"},
            "transcript": [
                {"role": "bot", "text": prompting.opening_line(CUSTOMER)},
                {"role": "customer", "text": "context:", "unclear": "hallucination_marker"},
                {"role": "bot", "text": prompting.identity_reask(CUSTOMER)},
                {"role": "customer", "text": "Yes."},
                {"role": "bot", "text": "Thanks. I'm just calling about your Mumbai Bank credit card - the payment is due."},
            ],
        }
        self.assertEqual(review_agent(record, CUSTOMER)["findings"], [])

    def test_merge_unclear_turns(self):
        transcript = [
            {"role": "bot", "text": prompting.opening_line(CUSTOMER)},
            {"role": "bot", "text": prompting.identity_reask(CUSTOMER)},
            {"role": "customer", "text": "Yes."},
        ]
        merged = merge_unclear_turns(transcript, [{"text": "context:", "unclear_reason": "hallucination_marker"},
                                                  {"text": "Yes.", "unclear_reason": None}])
        self.assertEqual([t["text"] for t in merged],
                         [prompting.opening_line(CUSTOMER), "context:", prompting.identity_reask(CUSTOMER), "Yes."])
        self.assertEqual(merged[1]["unclear"], "hallucination_marker")


if __name__ == "__main__":
    unittest.main()
