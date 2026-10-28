"""The live pipeline's new processors (bot/main.py, Execution Plan §16),
run inside a real Pipecat test pipeline.

Run with: uv run python -m unittest discover tests
"""

import unittest

from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test

from bot import prompting
from bot.main import CUSTOMERS, ClipRecorder, TurnGate, transcription_confidence
from bot.state import CallState

CUSTOMER = CUSTOMERS["MB-0007"]


def tx(text, logprobs=None):
    return TranscriptionFrame(text, "user", "2026-10-28T00:00:00Z", result={"logprobs": logprobs} if logprobs else None)


class TurnGateProcessorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = CallState()
        self.context = LLMContext()
        self.queued, self.sent, self.outcomes = [], [], []

        async def on_outcome(o):
            self.outcomes.append(o)

        async def send(m):
            self.sent.append(m)

        async def queue(frames):
            self.queued.extend(frames)

        self.gate = TurnGate(self.state, self.context, CUSTOMER, ClipRecorder(),
                             on_outcome=on_outcome, send_message=send, queue_frames=queue)

    async def test_unclear_turn_never_reaches_the_llm(self):
        down, _ = await run_test(self.gate, frames_to_send=[tx("context:"), tx("Yes."), tx("Yes, that's my card"),
                                                            tx("I just forgot, sorry")],
                                 expected_down_frames=[TranscriptionFrame])
        self.assertEqual(down[0].text, "I just forgot, sorry")  # the only turn the LLM answers
        spoken = [f.text for f in self.queued if isinstance(f, TTSSpeakFrame)]
        self.assertEqual(spoken[:2], [prompting.identity_reask(CUSTOMER), prompting.card_check_line(CUSTOMER)])
        self.assertIn(spoken[2], prompting.PURPOSE_LINES)
        self.assertEqual(self.sent, [{"type": "turn-unclear", "text": "context:", "reason": "hallucination_marker"}])
        self.assertTrue(self.state.identity_confirmed)
        self.assertEqual([t["gate"] for t in self.gate.stt_turns], ["reask_identity", "card_check", "purpose", None])

    async def test_waiver_ends_the_call(self):
        await run_test(self.gate, frames_to_send=[tx("Yes"), tx("Yes, my card"), tx("Please waive the late fee")],
                       expected_down_frames=[])
        self.assertEqual(self.outcomes[0]["type"], "callback_request")
        self.assertIsInstance(self.queued[-1], EndFrame)
        self.assertEqual(self.queued[-2].text, f"{prompting.WAIVER_LINE} {prompting.CLOSING_LINE}")
        # The LLM still learns the request was made.
        self.assertIn({"role": "user", "content": "Please waive the late fee"}, self.context.get_messages())

    async def test_amounts_are_spoken_as_words(self):
        await run_test(self.gate, frames_to_send=[tx("Yes"), tx("Yes, my card"), tx("What's my total balance?"),
                                                  tx("4 8 2 9 1 3")], expected_down_frames=[])
        self.assertIn("eighteen thousand four hundred twenty rupees", self.queued[-1].text)

    def test_confidence_from_logprobs(self):
        self.assertAlmostEqual(transcription_confidence(tx("yes", [{"logprob": 0.0}, {"logprob": 0.0}])), 1.0)
        self.assertIsNone(transcription_confidence(tx("yes")))


class ClipRecorderTest(unittest.IsolatedAsyncioTestCase):
    async def test_segments_follow_vad(self):
        rec = ClipRecorder()
        chunk = b"\x01\x00" * 1600  # 0.1 s at 16 kHz
        frames = [InputAudioRawFrame(chunk, 16000, 1) for _ in range(10)]
        frames += [VADUserStartedSpeakingFrame()]
        frames += [InputAudioRawFrame(chunk, 16000, 1) for _ in range(5)]
        frames += [VADUserStoppedSpeakingFrame()]
        await run_test(rec, frames_to_send=frames)
        self.assertEqual(len(rec.segments), 1)
        seg = rec.segments[0]
        self.assertEqual((seg["start"], seg["end"]), (0.5, 1.5))  # 0.5 s pre-roll before VAD start
        self.assertEqual(len(seg["audio"]), 16000 * 2)
        self.assertIs(rec.claim_latest(), seg)
        self.assertIsNone(rec.claim_latest())


if __name__ == "__main__":
    unittest.main()
