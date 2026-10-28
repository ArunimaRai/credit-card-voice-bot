"""Pipecat pipeline + WebRTC transport for the collections voicebot.

Cascade pipeline (STT -> LLM -> TTS) over SmallWebRTC, with the L3 state
machine and L4 guardrails around the LLM and the outcome tool wired to the
webhook. The disconnect handler always posts a webhook record, even when the
LLM never called record_outcome (D1's worst case — see Execution.md §11's
Day 1 findings).

Run with:
    uv run bot/main.py
"""

import asyncio
import json
import math
import os
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
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
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from bot import analysis, guardrails, turn_gate
from bot.prompting import PROMPT_VERSION, build_developer_message, build_system_prompt, opening_line, speakable
from bot.schema import validate_outcome
from bot.state import CallState
from bot.tools import RECORD_OUTCOME
from bot.webhook import post_outcome
from evals import live_cases


class GuardrailProcessor(FrameProcessor):
    """L4 pre-TTS guardrail (Execution.md §7). Buffers one full LLM
    turn's text (between LLMFullResponseStartFrame and ...EndFrame) rather
    than checking token-by-token, because several rules (e.g. "any rupee
    figure that isn't one of the customer's") need the whole utterance, not a fragment.
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
        self.triggers: list[dict] = []  # read by the post-call analysis

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
                    identity_confirmed=self._state.identity_confirmed,
                    customer=self._customer,
                    otp_verified=self._state.otp_verified,
                )
                if result.triggered:
                    logger.warning(f"L4 guardrail triggered: {result.rule} — substituting safe line")
                    self.triggers.append({"turn": self._state.turn_count, "rule": result.rule, "action": "substituted"})
                    text = result.safe_text
                    if result.rule == "no_balance_pre_otp":
                        self._state.otp_requested = True
                # Amounts as words for TTS, after the check has seen the digits
                # (voice-guide.md §4).
                text = speakable(text)

            if start_frame is not None:
                await self.push_frame(start_frame, direction)
            if text:
                await self.push_frame(LLMTextFrame(text), direction)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class LogprobRealtimeSTT(OpenAIRealtimeSTTService):
    """Streaming STT that also asks for token logprobs, so the junk-turn
    filter (bot/stt_filter.py) can use the model's own confidence. pipecat
    1.14 doesn't expose the session's `include` field, so it's added to the
    session.update message on its way out."""

    async def _ws_send(self, message: dict):
        if message.get("type") == "session.update":
            message["session"]["include"] = ["item.input_audio_transcription.logprobs"]
        await super()._ws_send(message)


def transcription_confidence(frame: TranscriptionFrame) -> float | None:
    """exp(mean token logprob) of a Realtime transcription, or None."""
    result = frame.result if isinstance(frame.result, dict) else {}
    lps = [lp.get("logprob") for lp in result.get("logprobs") or [] if lp.get("logprob") is not None]
    return round(math.exp(sum(lps) / len(lps)), 3) if lps else None


class ClipRecorder(FrameProcessor):
    """Keeps the customer audio of each VAD speech segment (Execution Plan
    §16.6), so every STT result can be paired with the audio it came from
    and saved as an eval clip. Sits before STT; the VAD frames reach it
    travelling upstream from the user aggregator."""

    PRE_ROLL_SEC = 0.5

    def __init__(self):
        super().__init__()
        self.segments: list[dict] = []  # {start, end, audio, sample_rate, paired}
        self._samples = 0
        self._rate = 0
        self._pre_roll = bytearray()
        self._current: dict | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._rate = frame.sample_rate
            n = len(frame.audio) // 2
            if self._current is not None:
                self._current["audio"].extend(frame.audio)
            else:
                self._pre_roll.extend(frame.audio)
                keep = int(self.PRE_ROLL_SEC * self._rate) * 2
                del self._pre_roll[:-keep]
            self._samples += n
        elif isinstance(frame, VADUserStartedSpeakingFrame) and self._rate:
            pre = len(self._pre_roll) // 2
            self._current = {"start": round((self._samples - pre) / self._rate, 2), "end": None,
                             "audio": bytearray(self._pre_roll), "sample_rate": self._rate, "paired": False}
            self._pre_roll.clear()
        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._current is not None:
            self._current["end"] = round(self._samples / self._rate, 2)
            self.segments.append(self._current)
            self._current = None
        await self.push_frame(frame, direction)

    def claim_latest(self) -> dict | None:
        """The most recent finished segment without a transcription yet."""
        for seg in reversed(self.segments):
            if not seg["paired"]:
                seg["paired"] = True
                return seg
        return None


class TurnGate(FrameProcessor):
    """Runs every final transcription through bot/turn_gate.py before the
    LLM sees it (Execution Plan §16.3-16.5). A turn that passes goes on to the
    LLM as before, after feeding the L3 state machine and any nudges into the
    context. A gated turn never reaches the LLM: its scripted line goes
    straight to TTS, and when the gate files an outcome the call ends after
    that line."""

    def __init__(self, state: CallState, context: LLMContext, customer: dict, clips: ClipRecorder,
                 on_outcome, send_message, queue_frames):
        super().__init__()
        self._state = state
        self._context = context
        self._customer = customer
        self._clips = clips
        self._on_outcome = on_outcome
        self._send_message = send_message
        self._queue_frames = queue_frames
        self.stt_turns: list[dict] = []  # every final STT result, kept with the call record
        self._ended = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not (isinstance(frame, TranscriptionFrame) and frame.text):
            await self.push_frame(frame, direction)
            return
        if self._ended:
            return

        confidence = transcription_confidence(frame)
        gate = turn_gate.gate_turn(self._state, frame.text, self._customer, confidence=confidence)
        seg = self._clips.claim_latest()
        self.stt_turns.append({
            "text": frame.text,
            "confidence": confidence,
            "unclear_reason": gate.unclear_reason if gate else None,
            "gate": gate.kind if gate else None,
            "start": seg["start"] if seg else None,
            "end": seg["end"] if seg else None,
            "_segment": seg,
        })

        if gate is None:
            for nudge in self._state.pending_nudges():
                logger.info(f"L3 state nudge: {nudge}")
                self._context.add_message({"role": "developer", "content": nudge})
            await self.push_frame(frame, direction)
            return

        logger.info(f"Turn gate: {gate.kind} ({gate.unclear_reason or 'clear'}) for {frame.text!r}")
        if gate.unclear_reason:
            await self._send_message({"type": "turn-unclear", "text": frame.text, "reason": gate.unclear_reason})
        else:
            # Clear speech the gate answered itself: the LLM still needs to
            # know it was said.
            self._context.add_message({"role": "user", "content": frame.text})
        frames: list[Frame] = [TTSSpeakFrame(speakable(gate.line))]
        if gate.outcome is not None:
            await self._on_outcome(gate.outcome)
        if gate.end_call:
            self._ended = True
            frames.append(EndFrame())
        await self._queue_frames(frames)

load_dotenv()  # does not override already-exported shell vars (e.g. `CUSTOMER_ID=MB-0014 uv run bot/main.py`)

CUSTOMER_ID = os.getenv("CUSTOMER_ID", "MB-0007")

CUSTOMERS = json.loads((Path(__file__).parent / "fixtures" / "customers.json").read_text())

# Chosen by ear on 2026-10-28 from four OpenAI female voices: "sage" with
# short, gentle instructions. A longer list of accent rules made "coral"
# sound stern. OpenAI has no real Indian-accent voice; the console says so.
TTS_INSTRUCTIONS = (
    "Speak as a friendly young woman from India, with a soft Indian English accent. "
    "Gentle, calm and smiling, like you are helping a friend. Never stern, never urgent. "
    "Relaxed pace with small natural pauses."
)

RECORDINGS_DIR =Path(__file__).resolve().parent.parent / "recordings" / "audio"

# Post-call analyses run after the pipeline is torn down, so they're held
# here rather than on the call's own tasks (and kept referenced until done).
_analysis_tasks: set[asyncio.Task] = set()


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info(f"Starting bot — prompt_version={PROMPT_VERSION}, customer={CUSTOMER_ID}")

    customer = CUSTOMERS.get(CUSTOMER_ID)

    # Streaming transcription with noise reduction and no hint prompt
    # (Execution Plan §16.3, D10). The old segmented setup's domain hint
    # prompt was the source of the made-up words seen live ("context:",
    # "late fee", "waiver"); the diagnostic run on 2026-10-28 traced every
    # one of them to it. far_field: demo users are mostly on laptop speakers.
    # Local Silero VAD still decides turns (turn_detection=False).
    stt = LogprobRealtimeSTT(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=LogprobRealtimeSTT.Settings(
            model=os.getenv("OPENAI_STT_MODEL", "gpt-transcribe"),
            noise_reduction="far_field",
        ),
    )
    tts = OpenAITTSService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAITTSService.Settings(
            model="gpt-4o-mini-tts",
            voice=os.getenv("OPENAI_VOICE_ID", "sage"),
            # gpt-4o-mini-tts takes acting instructions; the voice IDs alone
            # are all American-sounding.
            instructions=TTS_INSTRUCTIONS,
        ),
    )
    system_prompt = build_system_prompt(customer)
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
            system_instruction=system_prompt,
        ),
    )

    call_started_at = time.time()
    call_started_at_iso = datetime.now(timezone.utc).isoformat()
    call_state = {"outcome_recorded": False, "outcome_retries": 0, "outcome": None, "finished": False}
    call_state_machine = CallState()

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

        if customer is not None:
            error = validate_outcome(args, customer)
            if error is not None:
                call_state["outcome_retries"] += 1
                if call_state["outcome_retries"] <= 2:
                    logger.warning(f"record_outcome rejected ({call_state['outcome_retries']}): {error}")
                    await params.result_callback({"status": "rejected", "reason": error})
                    return
                logger.warning(f"record_outcome rejected {call_state['outcome_retries']}x, forcing no_outcome: {error}")
                args = {"type": "no_outcome", "reason": f"outcome rejected and never corrected: {error}"}

        if call_state["outcome_recorded"]:
            # The turn gate already filed one (glitch or waiver) and the call
            # is ending; the model doesn't get a second outcome.
            await params.result_callback({"status": "recorded"})
            return
        await file_outcome(args)
        await params.result_callback({"status": "recorded"})

    async def file_outcome(args: dict):
        logger.info(f"record_outcome: {args}")
        call_state["outcome_recorded"] = True
        call_state["outcome"] = args
        await post_outcome(base_payload(args))
        # Client-side console (client/index.html) needs this to show a real
        # confirmation/outcome badge instead of guessing from transcript text.
        # Not sent from on_client_disconnected's forced-no_outcome fallback
        # below: the data channel is already tearing down there, so the
        # client instead treats "disconnected with no call-outcome message
        # received" as the no_outcome case itself (same semantics, no race).
        await worker.rtvi.send_server_message({"type": "call-outcome", "outcome": args})

    raw_params = RECORD_OUTCOME["function"]["parameters"]
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

    dev_message = build_developer_message(customer)
    if dev_message:
        context.add_message({"role": "developer", "content": dev_message})

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    # Records the whole call to a stereo WAV (user on the left, bot on the
    # right) for the submission's call-recording deliverable.
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    recorded_audio = bytearray()
    recording_meta = {"sample_rate": 0, "num_channels": 2}
    audio_recorder = AudioBufferProcessor(num_channels=2, auto_start_recording=True)

    @audio_recorder.event_handler("on_audio_data")
    async def on_audio_data(_processor, audio: bytes, sample_rate: int, num_channels: int):
        recording_meta["sample_rate"] = sample_rate
        recording_meta["num_channels"] = num_channels
        recorded_audio.extend(audio)

    recording_path = RECORDINGS_DIR / f"{call_started_at_iso.replace(':', '-')}-{PROMPT_VERSION}-{runner_args.session_id}.wav"

    @audio_recorder.event_handler("on_recording_stopped")
    async def on_recording_stopped(_processor):
        if not recorded_audio or not recording_meta["sample_rate"]:
            return
        path = recording_path
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(recording_meta["num_channels"])
            wav_file.setsampwidth(2)
            wav_file.setframerate(recording_meta["sample_rate"])
            wav_file.writeframes(bytes(recorded_audio))
        logger.info(f"Call recording saved to {path}")

    clip_recorder = ClipRecorder()
    pipeline_steps = [transport.input(), clip_recorder, stt]
    gate_processor = None
    if customer is not None:
        gate_processor = TurnGate(
            call_state_machine, context, customer, clip_recorder,
            on_outcome=lambda outcome: file_outcome(outcome),
            send_message=lambda msg: worker.rtvi.send_server_message(msg),
            queue_frames=lambda frames: worker.queue_frames(frames),
        )
        pipeline_steps.append(gate_processor)
    pipeline_steps.append(user_aggregator)
    pipeline_steps.append(llm)
    guardrail_processor = None
    if customer is not None:
        guardrail_processor = GuardrailProcessor(call_state_machine, customer)
        pipeline_steps.append(guardrail_processor)
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
        # The console needs this id to fetch the post-call analysis once the
        # data channel is gone (GET /api/calls/{call_id}/analysis).
        await worker.rtvi.send_server_message({"type": "call-started", "call_id": runner_args.session_id})
        if customer is not None:
            # The opening line is fixed, so skip the LLM round trip (and the
            # guardrail buffer) and send it straight to TTS. Measured
            # 2026-10-28: that cuts ~1.1-1.7s of silence at pickup. It is
            # still appended to the context, so the LLM sees it was said.
            await worker.queue_frames([TTSSpeakFrame(opening_line(customer))])
        else:
            context.add_message(
                {"role": "developer", "content": "The call has connected. Begin the call now."}
            )
            await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

    async def finish_call():
        """Files the call record and starts the post-call analysis and eval
        capture. Runs once, whichever way the call ended: the customer hung
        up, or the turn gate ended it after a glitch or waiver line."""
        if call_state["finished"]:
            return
        call_state["finished"] = True
        if not call_state["outcome_recorded"]:
            # D1's worst case, confirmed live on 2026-10-28: a call can end
            # with nothing recorded at all — not even a fabricated promise,
            # just silence. A hangup must be filed as *something*, so the
            # transport layer — not the LLM — guarantees a record exists.
            logger.warning("Call ended with no outcome recorded — forcing no_outcome")
            call_state["outcome_recorded"] = True
            call_state["outcome"] = {"type": "no_outcome", "reason": "call ended without a recorded outcome"}
            await post_outcome(base_payload(call_state["outcome"]))

        stt_turns = gate_processor.stt_turns if gate_processor else []
        record = base_payload(call_state["outcome"])
        record.update(
            signals=sorted(call_state_machine.signals),
            identity_confirmed=call_state_machine.identity_confirmed,
            identity_denied=call_state_machine.identity_denied,
            guardrail_triggers=guardrail_processor.triggers if guardrail_processor else [],
            transcript=analysis.merge_unclear_turns(
                analysis.transcript_from_context(context.get_messages()), stt_turns),
            stt_turns=[{k: v for k, v in t.items() if not k.startswith("_")} for t in stt_turns],
        )
        segments = [t.get("_segment") for t in stt_turns]
        unpaired = [seg for seg in clip_recorder.segments if not seg["paired"]]
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        async def analyse_then_capture():
            entry = await analysis.run_analysis(client, record)
            await live_cases.capture_live_call(client, entry, segments, unpaired)

        task = asyncio.create_task(analyse_then_capture())
        _analysis_tasks.add(task)
        task.add_done_callback(_analysis_tasks.discard)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await finish_call()
        await runner.cancel()

    await runner.run()
    await finish_call()


async def bot(runner_args: RunnerArguments):
    """Entry point the dev runner discovers and calls per session."""
    transport_params = {
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from pipecat.runner.run import app as runner_app
    from pipecat.runner.run import main

    client_dir = Path(__file__).resolve().parent.parent / "client"

    @runner_app.get("/", include_in_schema=False)
    async def _serve_client():
        return FileResponse(client_dir / "index.html")

    @runner_app.get("/api/calls/{call_id}/analysis", include_in_schema=False)
    async def _call_analysis(call_id: str):
        entry = analysis.get_analysis(call_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown call")
        return entry

    # Guidelines, Personas and Analytics pages: recomputed from disk on every request.
    @runner_app.get("/pages.js", include_in_schema=False)
    async def _serve_pages():
        return FileResponse(client_dir / "pages.js", media_type="text/javascript")

    @runner_app.get("/api/insights/{page}", include_in_schema=False)
    async def _insights(page: str):
        from bot import insights

        views = {"guidelines": insights.guidelines, "personas": insights.personas, "analytics": insights.analytics}
        if page not in views:
            raise HTTPException(status_code=404, detail="unknown page")
        return views[page]()

    main()
