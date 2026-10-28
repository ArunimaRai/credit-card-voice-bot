"""Turns every real call into eval material (Execution Plan §16.6, D14).

After each call with customer speech in it (calls where the customer never
spoke are "null" and skipped), this module:

1. saves each STT segment's customer audio as a clip (data/clips/<call>/, local
   only: it is the speaker's own voice);
2. makes a reference transcript ("what was actually said") for each clip by
   letting three different models transcribe it and keeping what at least
   two agree on (see clip_reference). It is machine-made and labelled
   `reference_source: auto_model_vote`, never passed off as a human label;
3. writes the case to data/eval_cases/<call>.json, then rebuilds the three
   text eval sets in evals/live/ (committed, no audio):
   - stt_cases.jsonl: one row per clip, for evals/stt_eval.py;
   - replay_cases.jsonl: the customer turns as STT heard them, for
     evals/replay.py to feed back into the shipped bot;
   - promoted_personas.yaml: calls whose bot-side findings were critical or
     major, as new personas for evals/harness.py;
4. re-fits the STT filter's thresholds on the grown set (evals/stt_eval.py),
   the only self-learning the bot does (§16.7).

`python -m evals.live_cases --backfill` processes the recordings saved
before this existed.
"""

import argparse
import asyncio
import io
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np
import yaml
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot.analysis import CALLS_DIR, review_agent  # noqa: E402
from bot.stt_filter import words  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
CLIPS_DIR = DATA_DIR / "clips"
CASES_DIR = DATA_DIR / "eval_cases"
RECORDINGS_DIR = REPO_ROOT / "recordings" / "audio"
LIVE_DIR = REPO_ROOT / "evals" / "live"
STT_SET = LIVE_DIR / "stt_cases.jsonl"
REPLAY_SET = LIVE_DIR / "replay_cases.jsonl"
PROMOTED = LIVE_DIR / "promoted_personas.yaml"

BRIEF_MODEL = os.getenv("SIM_CUSTOMER_MODEL", "gpt-4.1-mini")
# Findings about the bot's own behaviour that a simulated customer can
# reproduce. STT-side findings stay in the STT and replay sets instead.
PROMOTABLE = {"pre_identity_topic", "identity_flow", "guardrail", "repeated_question", "no_outcome"}


# --- audio --------------------------------------------------------------

def read_customer_channel(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return rate, audio.reshape(-1, channels)[:, 0] if channels > 1 else audio


def wav_bytes(pcm: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


def energy_segments(x: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """Speech-or-noise stretches of a channel, for recordings made before
    clips were saved live. 20 ms frames, gaps under 0.8 s merged, under
    0.2 s dropped."""
    frame = rate // 50
    n = len(x) // frame
    if not n:
        return []
    rms = np.sqrt((x[: n * frame].astype(np.float32).reshape(n, frame) ** 2).mean(1))
    on = rms > max(300.0, float(np.percentile(rms, 60)) * 3)
    segs, start, last = [], None, None
    for i, v in enumerate(on):
        if not v:
            continue
        if start is None:
            start = i
        elif i - last > 40:
            segs.append((start, last + 1))
            start = i
        last = i
    if start is not None:
        segs.append((start, last + 1))
    return [(round(s * 0.02, 2), round(e * 0.02, 2)) for s, e in segs if e - s >= 10]


# --- reference transcript ----------------------------------------------

# Three different models transcribe every clip; their agreement is the label.
# A single full-call pass (tried first, whisper-1 with word timestamps) loops
# and invents phrases on a mostly silent channel ("If you have any questions
# please contact me", "Hello" x12). Real speech comes back the same from
# different models; noise comes back as different junk, or nothing.
REFERENCE_MODELS = ("gpt-4o-transcribe", "whisper-1", "gpt-4o-mini-transcribe")


async def _transcribe_clip(client, model: str, wav: bytes) -> str:
    f = io.BytesIO(wav)
    f.name = "clip.wav"
    try:
        r = await client.audio.transcriptions.create(model=model, file=f)
        return r.text.strip()
    except Exception as e:
        logger.warning(f"Reference transcription with {model} failed: {e}")
        return ""


# Models spell the same short answer differently ("Yes." / "Yeah."); that
# is agreement, not a split vote.
_SAME = {"yeah": "yes", "yep": "yes", "yup": "yes", "ya": "yes", "ok": "okay", "haan": "yes", "han": "yes"}


def _vocab(text: str) -> set[str]:
    return {_SAME.get(w, w) for w in words(text)}


def _agree(a: str, b: str) -> bool:
    wa, wb = _vocab(a), _vocab(b)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.5


async def clip_reference(client, pcm: np.ndarray, rate: int) -> tuple[str, dict]:
    """(reference text, each model's output). The reference is the first
    model's text among those that agree with at least one other; "" means
    noise."""
    wav = wav_bytes(pcm, rate)
    outs = await asyncio.gather(*(_transcribe_clip(client, m, wav) for m in REFERENCE_MODELS))
    votes = dict(zip(REFERENCE_MODELS, outs))
    return vote(votes), votes


def vote(votes: dict) -> str:
    outs = list(votes.values())
    for i, a in enumerate(outs):
        if any(_agree(a, b) for j, b in enumerate(outs) if j != i):
            return a
    return ""


# --- cases --------------------------------------------------------------

def _short(call_id: str) -> str:
    return call_id.split("-")[0]


async def build_case(client, call_id: str, record: dict | None, clips: list[dict], source: str) -> dict | None:
    """clips: [{start, end, pcm, rate, live}] where live is the STT result the
    call actually got for that clip ({text, confidence, unclear_reason}) or
    None when it isn't known (backfilled calls). Returns None for a null call."""
    refs = await asyncio.gather(*(clip_reference(client, c["pcm"], c["rate"]) for c in clips))
    customer_turns = [t for t in (record or {}).get("transcript", []) if t["role"] == "customer"]
    if not customer_turns and not any(words(r) for r, _ in refs):
        logger.info(f"Eval capture: {call_id} is a null call (customer never spoke), skipped")
        return None

    clip_dir = CLIPS_DIR / call_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (c, (reference, votes)) in enumerate(zip(clips, refs)):
        path = clip_dir / f"{i:02d}.wav"
        path.write_bytes(wav_bytes(c["pcm"], c["rate"]))
        rows.append({
            "clip": str(path.relative_to(REPO_ROOT)),
            "start": c["start"],
            "end": c["end"],
            "duration": round(c["end"] - c["start"], 2),
            "reference": reference,
            "label": "speech" if words(reference) else "noise",
            "reference_votes": votes,
            "live": c.get("live"),
        })

    findings = review_agent(record)["findings"] if record else []
    case = {
        "call_id": call_id,
        "source": source,
        "customer_id": (record or {}).get("customer_id"),
        "prompt_version": (record or {}).get("prompt_version"),
        "reference_source": "auto_model_vote",
        "reference": " ".join(r["reference"] for r in rows if r["reference"]),
        "clips": rows,
        "customer_turns": [
            {"text": t["text"], "confidence": t.get("confidence"), "unclear_reason": t.get("unclear_reason")}
            for t in ((record or {}).get("stt_turns") or customer_turns)
        ],
        "outcome": (record or {}).get("outcome"),
        "findings": [{k: f[k] for k in ("key", "severity", "title", "turn", "quote")} for f in findings],
    }
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    (CASES_DIR / f"{call_id}.json").write_text(json.dumps(case, indent=2, ensure_ascii=False))
    logger.info(f"Eval capture: {call_id} -> {len(rows)} clips, {len(findings)} findings")
    return case


def load_cases() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(CASES_DIR.glob("*.json"))]


def rebuild_sets(cases: list[dict] | None = None) -> None:
    """Regenerates the committed text sets from every local case."""
    cases = load_cases() if cases is None else cases
    for case in cases:  # relabel from the stored votes, so a voting change reaches old clips
        for clip in case["clips"]:
            if clip.get("reference_votes"):
                clip["reference"] = vote(clip["reference_votes"])
                clip["label"] = "speech" if words(clip["reference"]) else "noise"
        case["reference"] = " ".join(c["reference"] for c in case["clips"] if c["reference"])
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    with STT_SET.open("w") as f:
        for case in cases:
            for clip in case["clips"]:
                f.write(json.dumps({"call_id": case["call_id"], **clip}, ensure_ascii=False) + "\n")
    with REPLAY_SET.open("w") as f:
        for case in cases:
            if not case["customer_turns"]:
                continue
            f.write(json.dumps({
                "call_id": case["call_id"],
                "customer_id": case["customer_id"] or "MB-0007",
                "turns": case["customer_turns"],
                "live_findings": sorted({x["key"] for x in case["findings"]}),
            }, ensure_ascii=False) + "\n")
    promoted = [c["persona"] for c in cases if c.get("persona")]
    PROMOTED.write_text(
        "# Personas promoted from live calls (evals/live_cases.py). src: L = found live.\n"
        "# Generated: edit the source call's case, not this file.\n"
        + (yaml.safe_dump(promoted, sort_keys=False, allow_unicode=True) if promoted else "[]\n")
    )


BRIEF_PROMPT = """Below is what a bank customer actually said on a recorded collections call \
(their side only, machine-transcribed). Write a 2-3 sentence brief for an actor who will play \
this customer in a test call: how they answer, what they ask, their tone. Second person \
("You ..."). Describe behaviour only; do not quote the bot or name the bank.

Customer said: {reference}"""


async def promote(client, case: dict) -> None:
    """Adds a persona to the case when the bot made a critical or major
    mistake a simulated customer could provoke again."""
    keys = {f["key"] for f in case["findings"] if f["severity"] in ("critical", "major") and f["key"] in PROMOTABLE}
    if not keys or not case["reference"].strip():
        return
    r = await client.chat.completions.create(
        model=BRIEF_MODEL, temperature=0,
        messages=[{"role": "user", "content": BRIEF_PROMPT.format(reference=case["reference"])}],
    )
    worst = "critical" if any(f["severity"] == "critical" and f["key"] in keys for f in case["findings"]) else "major"
    case["persona"] = {
        "id": f"L-{_short(case['call_id'])}",
        "archetype": "live-" + "+".join(sorted(keys)),
        "difficulty": "hard",
        "src": "L",
        "from_call": case["call_id"],
        "customer_brief": (r.choices[0].message.content or "").strip(),
        "assert": {"must_not_say": ["cibil", "credit score", "legal action"]},
        "severity_override": worst,
    }
    (CASES_DIR / f"{case['call_id']}.json").write_text(json.dumps(case, indent=2, ensure_ascii=False))


async def finish(client, case: dict | None) -> None:
    if case is None:
        return
    await promote(client, case)
    rebuild_sets()
    from evals import stt_eval  # late: stt_eval imports this module
    stt_eval.tune(apply=True)


# --- entry points -------------------------------------------------------

async def capture_live_call(client, entry: dict, segments: list[dict | None], unpaired: list[dict]) -> None:
    """Called by bot/main.py after the post-call analysis. segments[i] is the
    clip for record["stt_turns"][i] (None if VAD and STT didn't line up);
    unpaired are clips STT returned nothing for, kept as noise examples."""
    try:
        record = entry["record"]
        clips = []
        for turn, seg in zip(record.get("stt_turns") or [], segments):
            if seg and seg.get("audio"):
                clips.append({"start": seg["start"], "end": seg["end"], "rate": seg["sample_rate"],
                              "pcm": np.frombuffer(bytes(seg["audio"]), dtype=np.int16),
                              "live": {k: turn.get(k) for k in ("text", "confidence", "unclear_reason")}})
        for seg in unpaired:
            if seg.get("audio") and seg.get("end"):
                clips.append({"start": seg["start"], "end": seg["end"], "rate": seg["sample_rate"],
                              "pcm": np.frombuffer(bytes(seg["audio"]), dtype=np.int16),
                              "live": {"text": "", "confidence": None, "unclear_reason": "empty"}})
        case = await build_case(client, record["call_id"], record, clips, source="live")
        await finish(client, case)
    except Exception as e:  # eval capture must never break the call flow
        logger.exception(f"Eval capture failed for {entry.get('call_id')}: {e}")


async def backfill(client) -> None:
    """Cases for recordings saved before live capture existed. Their clips
    come from energy segmentation; per-clip live STT text isn't known."""
    for wav_path in sorted(RECORDINGS_DIR.glob("*.wav")):
        call_id = wav_path.stem.split("-v1-")[-1]
        if (CASES_DIR / f"{call_id}.json").exists():
            continue
        rec_path = CALLS_DIR / f"{call_id}.json"
        record = json.loads(rec_path.read_text()).get("record") if rec_path.exists() else None
        rate, pcm = read_customer_channel(wav_path)
        clips = [{"start": s, "end": e, "rate": rate, "pcm": pcm[int(s * rate):int(e * rate)], "live": None}
                 for s, e in energy_segments(pcm, rate)]
        case = await build_case(client, call_id, record, clips, source="backfill")
        if case:
            await promote(client, case)
    rebuild_sets()


if __name__ == "__main__":
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="Process recordings/audio/ without a case yet")
    parser.add_argument("--rebuild", action="store_true", help="Only regenerate evals/live/ from local cases")
    args = parser.parse_args()
    if args.rebuild:
        rebuild_sets()
    else:
        asyncio.run(backfill(AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))))
