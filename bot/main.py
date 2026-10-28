"""Pipecat pipeline + WebRTC transport for the collections voicebot.

Day 1 scope: cascade pipeline (STT -> LLM -> TTS) over SmallWebRTC, running
whichever prompt PROMPT_VERSION selects, with the outcome tool wired to the
webhook. No content guardrails (L4), no identity/OTP/de-escalation state
machine (L3) yet — those are Day 3 (Execution.md §11). The one exception is
record integrity: the disconnect handler always posts a webhook record, even
when the LLM never called record_outcome (D1's worst case — see §11's Day 1
findings). That's a transport-level safety net, not a content guardrail, so
it applies uniformly to v1/v2/v3 without touching what any version is
allowed to say.

Run with:
    uv run bot/main.py
"""

import json
import os
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMRunFrame, LLMTextFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from bot import guardrails
from bot.prompting import build_developer_message, build_system_prompt
from bot.schema import validate_outcome
from bot.state import CallState
from bot.tools import TOOLS_BY_VERSION
from bot.webhook import post_outcome


class GuardrailProcessor(FrameProcessor):
    """L4 pre-TTS guardrail (Execution.md §7), v3 only. Buffers one full LLM
    turn's text (between LLMFullResponseStartFrame and ...EndFrame) rather
    than checking token-by-token, because several rules (e.g. "any rupee
    figure that isn't min_due") need the whole utterance, not a fragment.
    This trades some of the cascade's incremental-TTS latency for a
    guardrail that can actually see what it's checking — a deliberate,
    documented cost (see writeup "what I'd cut"), and the reason the live
    pipeline runs the deterministic rule pass only, not the eval harness's
    extra LLM semantic check (that would cost a second model round trip on
    every single turn, live).
    """

    def __init__(self, state: CallState, customer: dict):
        super().__init__()
        self._state = state
        self._customer = customer
        self._buffering = False
        self._buffer = ""
        self._start_frame: LLMFullResponseStartFrame | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buffering = True
            self._buffer = ""
            self._start_frame = frame
            return

        if self._buffering and isinstance(frame, LLMTextFrame):
            self._buffer += frame.text
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            text = self._buffer
            start_frame = self._start_frame
            self._buffering = False
            self._buffer = ""
            self._start_frame = None

            if text:
                result = guardrails.check_deterministic(
                    text,
                    otp_verified=self._state.otp_verified,
                    identity_confirmed=self._state.identity_confirmed,
                    min_due=self._customer["min_due"],
                )
                if result.triggered:
                    logger.warning(f"L4 guardrail triggered: {result.rule} — substituting safe line")
                    text = result.safe_text

            if start_frame is not None:
                await self.push_frame(start_frame, direction)
            if text:
                await self.push_frame(LLMTextFrame(text), direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class StateWatcher(FrameProcessor):
    """Feeds each committed customer transcription into the L3 state
    machine (Execution.md §8) and injects any newly-crossed-threshold nudge
    straight into the shared LLM context, the same way on_client_ready
    already injects "the call has connected" below. v3 only."""

    def __init__(self, state: CallState, context: LLMContext, customer: dict):
        super().__init__()
        self._state = state
        self._context = context
        self._customer = customer

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            self._state.note_turn()
            self._state.observe_customer_turn(frame.text, expected_otp=self._customer.get("otp"))
            for nudge in self._state.pending_nudges():
                logger.info(f"L3 state nudge: {nudge}")
                self._context.add_message({"role": "developer", "content": nudge})
        await self.push_frame(frame, direction)

load_dotenv()  # does not override already-exported shell vars (e.g. `PROMPT_VERSION=v3 uv run bot/main.py`)

PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")
CUSTOMER_ID = os.getenv("CUSTOMER_ID", "MB-0007")

CUSTOMERS = json.loads((Path(__file__).parent / "fixtures" / "customers.json").read_text())

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings" / "audio"


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info(f"Starting bot — prompt_version={PROMPT_VERSION}, customer={CUSTOMER_ID}")

    customer = CUSTOMERS.get(CUSTOMER_ID)

    stt = OpenAISTTService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAISTTService.Settings(
            model=os.getenv("OPENAI_STT_MODEL", "gpt-4o-transcribe"),
            # Bias short/ambiguous utterances toward the expected domain and
            # language. Day 1 live test: "late fee" (~1.5s audio) was
            # transcribed as Urdu script even with language=EN already
            # pinned (pipecat's own default) — this is a mitigation for that,
            # not a guaranteed fix. Re-check on Day 2's Hinglish persona.
            prompt=(
                "This is a phone call in English between a Mumbai Bank collections agent "
                "and a credit card customer. Expect terms like: late fee, minimum due, "
                "outstanding balance, due date, promise to pay, OTP, waiver, callback."
            ),
        ),
    )
    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAITTSService.Settings(voice=os.getenv("OPENAI_VOICE_ID", "alloy")),
    )
    system_prompt = build_system_prompt(PROMPT_VERSION, customer)
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
            system_instruction=system_prompt,
        ),
    )

    call_started_at = time.time()
    call_started_at_iso = datetime.now(timezone.utc).isoformat()
    call_state = {"outcome_recorded": False, "outcome_retries": 0}
    call_state_machine = CallState() if PROMPT_VERSION == "v3" else None

    def base_payload(outcome: dict) -> dict:
        return {
            "call_id": runner_args.session_id,
            "customer_id": CUSTOMER_ID,
            "started_at": call_started_at_iso,
            "duration_sec": round(time.time() - call_started_at, 1),
            "prompt_version": PROMPT_VERSION,
            "outcome": outcome,
        }

    async def record_outcome(params):
        args = dict(params.arguments)

        if PROMPT_VERSION == "v3" and customer is not None:
            error = validate_outcome(args, customer)
            if error is not None:
                call_state["outcome_retries"] += 1
                if call_state["outcome_retries"] <= 2:
                    logger.warning(f"record_outcome rejected ({call_state['outcome_retries']}): {error}")
                    await params.result_callback({"status": "rejected", "reason": error})
                    return
                logger.warning(f"record_outcome rejected {call_state['outcome_retries']}x, forcing no_outcome: {error}")
                args = {"type": "no_outcome", "reason": f"outcome rejected and never corrected: {error}"}

        logger.info(f"record_outcome: {args}")
        call_state["outcome_recorded"] = True
        await post_outcome(base_payload(args))
        # Client-side console (client/index.html) needs this to show a real
        # confirmation/outcome badge instead of guessing from transcript text.
        # Not sent from on_client_disconnected's forced-no_outcome fallback
        # below: the data channel is already tearing down there, so the
        # client instead treats "disconnected with no call-outcome message
        # received" as the no_outcome case itself (same semantics, no race).
        await worker.rtvi.send_server_message({"type": "call-outcome", "outcome": args})
        await params.result_callback({"status": "recorded"})

    raw_params = TOOLS_BY_VERSION[PROMPT_VERSION][0]["function"]["parameters"]
    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="record_outcome",
                description="Record the result of this call. Call this exactly once, at the end of the call.",
                properties=raw_params["properties"],
                required=raw_params["required"],
                handler=record_outcome,
            )
        ]
    )

    context = LLMContext(tools=tools)

    dev_message = build_developer_message(PROMPT_VERSION, customer)
    if dev_message:
        context.add_message({"role": "developer", "content": dev_message})

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    # Records the whole call to a stereo WAV (user on the left, bot on the
    # right) for the submission's call-recording deliverable. Not v3-gated —
    # any version's demo call is worth capturing as audio, not just a text
    # transcript (see Progress Tracker's "no audio recording capture exists
    # yet" open item).
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    recorded_audio = bytearray()
    recording_meta = {"sample_rate": 0, "num_channels": 2}
    audio_recorder = AudioBufferProcessor(num_channels=2, auto_start_recording=True)

    @audio_recorder.event_handler("on_audio_data")
    async def on_audio_data(_processor, audio: bytes, sample_rate: int, num_channels: int):
        recording_meta["sample_rate"] = sample_rate
        recording_meta["num_channels"] = num_channels
        recorded_audio.extend(audio)

    @audio_recorder.event_handler("on_recording_stopped")
    async def on_recording_stopped(_processor):
        if not recorded_audio or not recording_meta["sample_rate"]:
            return
        path = RECORDINGS_DIR / f"{call_started_at_iso.replace(':', '-')}-{PROMPT_VERSION}-{runner_args.session_id}.wav"
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(recording_meta["num_channels"])
            wav_file.setsampwidth(2)
            wav_file.setframerate(recording_meta["sample_rate"])
            wav_file.writeframes(bytes(recorded_audio))
        logger.info(f"Call recording saved to {path}")

    pipeline_steps = [transport.input(), stt]
    if call_state_machine is not None and customer is not None:
        pipeline_steps.append(StateWatcher(call_state_machine, context, customer))
    pipeline_steps.append(user_aggregator)
    pipeline_steps.append(llm)
    if call_state_machine is not None and customer is not None:
        pipeline_steps.append(GuardrailProcessor(call_state_machine, customer))
    pipeline_steps += [
        tts,
        audio_recorder,
        transport.output(),
        assistant_aggregator,
    ]

    pipeline = Pipeline(pipeline_steps)

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[],
    )

    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        context.add_message(
            {"role": "developer", "content": "The call has connected. Begin the call now."}
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        if not call_state["outcome_recorded"]:
            # D1's worst case, confirmed live on 2026-10-28: a call can end
            # with nothing recorded at all — not even a fabricated promise,
            # just silence. A hangup must be filed as *something*, so the
            # transport layer — not the LLM — guarantees a record exists.
            logger.warning("Call ended with no outcome recorded — forcing no_outcome")
            call_state["outcome_recorded"] = True
            await post_outcome(
                base_payload({"type": "no_outcome", "reason": "call ended without a recorded outcome"})
            )
        await runner.cancel()

    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Entry point the dev runner discovers and calls per session."""
    transport_params = {
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from fastapi.responses import FileResponse

    from pipecat.runner.run import app as runner_app
    from pipecat.runner.run import main

    client_dir = Path(__file__).resolve().parent.parent / "client"

    @runner_app.get("/", include_in_schema=False)
    async def _serve_client():
        return FileResponse(client_dir / "index.html")

    main()
