"""Transcribe one audio clip through OpenAI's streaming (Realtime)
transcription API, configured the way the live bot is.

The live pipeline streams audio into a Realtime transcription session and
commits the buffer when local VAD says the customer stopped. This does the
same with a saved clip, so evals/stt_eval.py scores the STT the bot actually
runs, not the file-upload endpoint (which has no noise reduction).
"""

import asyncio
import base64
import json
import math

import websockets

URL = "wss://api.openai.com/v1/realtime?intent=transcription"
RATE = 24000  # the Realtime API only takes 24 kHz 16-bit mono PCM


def session_update(model: str, *, language: str | None, noise_reduction: str | None, logprobs: bool) -> dict:
    transcription: dict = {"model": model}
    if language:
        transcription["language"] = language
    audio_in: dict = {
        "format": {"type": "audio/pcm", "rate": RATE},
        "transcription": transcription,
        "turn_detection": None,
    }
    if noise_reduction:
        audio_in["noise_reduction"] = {"type": noise_reduction}
    session: dict = {"type": "transcription", "audio": {"input": audio_in}}
    if logprobs:
        session["include"] = ["item.input_audio_transcription.logprobs"]
    return {"type": "session.update", "session": session}


def mean_logprob(evt: dict) -> float | None:
    lps = [lp.get("logprob") for lp in evt.get("logprobs") or [] if lp.get("logprob") is not None]
    return sum(lps) / len(lps) if lps else None


async def transcribe(pcm: bytes, api_key: str, *, model: str, language: str | None = "en",
                     noise_reduction: str | None = "far_field", logprobs: bool = True,
                     timeout: float = 30.0) -> dict:
    """pcm: 24 kHz 16-bit mono. Returns {"text", "confidence"} where
    confidence is exp(mean token logprob), or None when the model gives none."""
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {api_key}"}) as ws:
        await ws.send(json.dumps(session_update(model, language=language,
                                                noise_reduction=noise_reduction, logprobs=logprobs)))
        for i in range(0, len(pcm), RATE):  # 0.5 s chunks
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(pcm[i:i + RATE]).decode()}))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        async def wait_done():
            async for raw in ws:
                evt = json.loads(raw)
                kind = evt.get("type", "")
                if kind == "conversation.item.input_audio_transcription.completed":
                    lp = mean_logprob(evt)
                    return {"text": evt.get("transcript", "").strip(),
                            "confidence": round(math.exp(lp), 3) if lp is not None else None}
                if kind in ("error", "conversation.item.input_audio_transcription.failed"):
                    raise RuntimeError(json.dumps(evt.get("error") or evt))

        return await asyncio.wait_for(wait_done(), timeout)
