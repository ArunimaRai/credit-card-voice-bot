"""Rule-based half of the post-call analysis (bot/analysis.py).

Run with: uv run python -m unittest discover tests
"""

import unittest
from datetime import date

from bot.analysis import derive_workflow, ground_evidence, resolve_follow_up_date, transcript_from_context

TODAY = date(2026, 10, 28)  # a Wednesday


def wf(outcome, signals=(), confirmed=True, denied=False, triggers=None):
    return derive_workflow(
        outcome, list(signals), identity_confirmed=confirmed, identity_denied=denied,
        guardrail_triggers=triggers, today=TODAY,
    )


class FollowUpDateTest(unittest.TestCase):
    def test_phrases(self):
        cases = {
            "tomorrow": date(2026, 10, 29),
            "day after tomorrow": date(2026, 10, 30),
            "this evening": TODAY,
            "Friday": date(2026, 10, 30),
            "next Monday": date(2026, 11, 2),
            "wednesday": date(2026, 11, 4),  # today is Wednesday -> next week's
            "next week": date(2026, 11, 2),
            "in 3 days": date(2026, 10, 31),
            "after two weeks": date(2026, 11, 11),
            "this weekend": date(2026, 10, 31),
            "end of the month": date(2026, 10, 31),
            "5th November": date(2026, 11, 5),
            "Nov 5": date(2026, 11, 5),
            "2026-11-03": date(2026, 11, 3),
            "1st of October": date(2027, 10, 1),  # past date this year rolls forward
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolve_follow_up_date(text, TODAY), expected)

    def test_vague_is_none(self):
        for text in ["soon", "after salary", "when I can", "", None, "may be later"]:
            with self.subTest(text=text):
                self.assertIsNone(resolve_follow_up_date(text, TODAY))


class WorkflowTest(unittest.TestCase):
    def test_promise_to_pay(self):
        r = wf({"type": "promise_to_pay", "date": "2026-11-04", "amount": 4820})
        self.assertEqual(r["stage"]["key"], "ptp_pending")
        self.assertEqual(r["stage"]["team"], "Payment verification")
        self.assertEqual(r["follow_up"]["date"], "2026-11-05")
        self.assertEqual(r["follow_up"]["date_source"], "promise_date")
        self.assertEqual(r["outcome"]["detail"], "₹4,820 by 2026-11-04")

    def test_already_paid(self):
        r = wf({"type": "already_paid", "mode": "UPI", "date": "2026-10-27"})
        self.assertEqual(r["stage"]["key"], "paid_pending_verification")
        self.assertEqual(r["follow_up"]["date"], "2026-10-30")  # +2 business days

    def test_callback_with_date(self):
        r = wf({"type": "callback_request", "requested_time": "tomorrow evening"})
        self.assertEqual(r["stage"]["key"], "callback_scheduled")
        self.assertEqual(r["follow_up"]["date"], "2026-10-29")
        self.assertEqual(r["follow_up"]["date_source"], "customer")

    def test_callback_vague_uses_default_sla(self):
        r = wf({"type": "callback_request", "requested_time": "soon"})
        self.assertEqual(r["follow_up"]["date"], "2026-10-30")
        self.assertEqual(r["follow_up"]["date_source"], "default_sla")

    def test_default_sla_skips_weekend(self):
        r = derive_workflow({"type": "callback_request", "requested_time": "soon"}, [],
                            identity_confirmed=True, identity_denied=False, today=date(2026, 10, 30))
        self.assertEqual(r["follow_up"]["date"], "2026-11-03")  # Fri + 2 business days = Tue

    def test_dispute(self):
        r = wf({"type": "dispute_transfer", "reason": "fee charged twice", "category": "billing"})
        self.assertEqual(r["stage"]["key"], "dispute")
        self.assertFalse(r["follow_up"]["needed"])

    def test_no_outcome_variants(self):
        no = {"type": "no_outcome", "reason": "x"}
        self.assertEqual(wf(no, ["identity_unverified"], confirmed=False, denied=True)["stage"]["key"], "wrong_party")
        self.assertEqual(wf(no, confirmed=False)["stage"]["key"], "identity_unverified")
        r = wf(no)
        self.assertEqual(r["stage"]["key"], "contacted_no_commitment")
        self.assertEqual(r["follow_up"]["date"], "2026-10-29")
        self.assertEqual(r["follow_up"]["date_source"], "retry_policy")

    def test_hardship_overrides(self):
        r = wf({"type": "callback_request", "requested_time": "tomorrow"}, ["hardship_disclosed"])
        self.assertEqual(r["stage"]["key"], "hardship_review")
        self.assertEqual(r["stage"]["overridden_from"], "Callback scheduled")
        self.assertFalse(r["follow_up"]["needed"])
        self.assertEqual(r["outcome"]["type"], "callback_request")  # original outcome kept

    def test_transfer_category_routes(self):
        for category, stage in [("abuse", "escalated"), ("legal", "escalated"),
                                ("hardship", "hardship_review"), ("dispute", "dispute")]:
            with self.subTest(category=category):
                r = wf({"type": "dispute_transfer", "reason": "r", "category": category})
                self.assertEqual(r["stage"]["key"], stage)

    def test_escalation_beats_hardship(self):
        for signals, triggers in [(["abuse_detected", "hardship_disclosed"], None),
                                  (["legal_threat"], None),
                                  ([], [{"turn": 2, "rule": "no_threats", "action": "substituted"}])]:
            with self.subTest(signals=signals, triggers=triggers):
                r = wf({"type": "promise_to_pay", "date": "2026-11-04", "amount": 4820}, signals, triggers=triggers)
                self.assertEqual(r["stage"]["key"], "escalated")
                self.assertEqual(r["stage"]["team"], "Compliance review")
        flags = [f["key"] for f in wf({"type": "no_outcome", "reason": "x"}, [], triggers=[{"turn": 1}])["flags"]]
        self.assertIn("guardrail_triggered", flags)

    def test_every_outcome_and_signal_combination_resolves(self):
        outcomes = [
            {"type": "promise_to_pay", "date": "2026-11-04", "amount": 4820},
            {"type": "already_paid", "mode": "UPI", "date": "2026-10-27"},
            {"type": "callback_request", "requested_time": "soon"},
            {"type": "dispute_transfer", "reason": "r", "category": "c"},
            {"type": "no_outcome", "reason": "r"},
        ]
        signal_sets = [[], ["hardship_disclosed"], ["abuse_detected"], ["legal_threat"], ["otp_failed"], ["identity_unverified"]]
        for o in outcomes:
            for sig in signal_sets:
                for confirmed, denied in [(True, False), (False, False), (False, True)]:
                    r = wf(o, sig, confirmed, denied)
                    self.assertTrue(r["stage"]["label"])
                    if r["follow_up"]["needed"]:
                        self.assertIsNotNone(r["follow_up"]["date"])
                        self.assertGreaterEqual(r["follow_up"]["date"], TODAY.isoformat())


class GroundingTest(unittest.TestCase):
    transcript = [
        {"role": "bot", "text": "Am I speaking to Rakesh?"},
        {"role": "customer", "text": "Yes. I already told you, I'll pay on Friday!"},
        {"role": "bot", "text": "Thank you."},
    ]

    def test_keeps_real_quote_and_fixes_turn(self):
        out = ground_evidence([{"turn": 0, "quote": "I already told you, I'll pay"}], self.transcript)
        self.assertEqual(out, [{"turn": 1, "quote": "I already told you, I'll pay"}])

    def test_drops_invented_quote_and_bot_quote(self):
        self.assertEqual(ground_evidence([{"turn": 1, "quote": "this is harassment"}], self.transcript), [])
        self.assertEqual(ground_evidence([{"turn": 2, "quote": "Thank you"}], self.transcript), [])


class TranscriptFromContextTest(unittest.TestCase):
    def test_keeps_spoken_turns_only(self):
        messages = [
            {"role": "developer", "content": "facts"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
            {"role": "assistant", "content": None, "tool_calls": [{}]},
            {"role": "tool", "content": "{}"},
        ]
        self.assertEqual(transcript_from_context(messages),
                         [{"role": "bot", "text": "Hello"}, {"role": "customer", "text": "Hi"}])


if __name__ == "__main__":
    unittest.main()
