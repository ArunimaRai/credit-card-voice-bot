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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from bot.prompts import v1, v2, v3
from bot.tools import TOOLS_BY_VERSION
from bot.webhook import post_outcome

load_dotenv(override=True)

PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")
CUSTOMER_ID = os.getenv("CUSTOMER_ID", "MB-0007")

CUSTOMERS = json.loads((Path(__file__).parent / "fixtures" / "customers.json").read_text())
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
    call_state = {"outcome_recorded": False}

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
        logger.info(f"record_outcome: {args}")
        call_state["outcome_recorded"] = True
        await post_outcome(base_payload(args))
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

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

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
