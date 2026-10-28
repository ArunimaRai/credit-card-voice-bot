"""Scores speech-to-text setups on the live STT set (Execution Plan §16.6),
and tunes the junk-turn filter on it (§16.7).

Every clip in evals/live/stt_cases.jsonl is real customer audio from a call,
labelled speech or noise by its reference transcript. For each STT setup this
reports, raw and after the filter (bot/stt_filter.py):
- junk_passed: noise clips that came back as text and would reach the bot;
- speech_dropped: speech clips that came back empty or got filtered out;
- wer: word error rate on speech clips;
- false_keywords: domain words ("waiver", "late fee", ...) in the output but
  not in the reference — the failure that escalated a live call.

    uv run python -m evals.stt_eval                      # all setups
    uv run python -m evals.stt_eval --config rt:gpt-transcribe:en
    uv run python -m evals.stt_eval --tune               # re-fit the filter

STT outputs are cached in data/stt_cache.json, so re-scoring and tuning cost
no API calls. Results go to evals/live/stt_results.csv.
"""

import argparse
import asyncio
import csv
import io
import itertools
import json
import os
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot import stt_filter  # noqa: E402
from bot.stt_filter import unclear_reason, words  # noqa: E402
from evals.live_cases import LIVE_DIR, STT_SET  # noqa: E402

CACHE = REPO_ROOT / "data" / "stt_cache.json"
RESULTS = LIVE_DIR / "stt_results.csv"
TUNING_LOG = LIVE_DIR / "tuning_log.jsonl"

# The setup the bot ran until 2026-10-28: segmented upload, domain hint prompt.
OLD_HINT = (
    "This is a phone call in English between a Mumbai Bank collections agent "
    "and a credit card customer. Expect terms like: late fee, minimum due, "
    "outstanding balance, due date, promise to pay, OTP, waiver, callback."
)
CONFIGS = [
    "old:gpt-4o-transcribe:hint",
    "rt:gpt-transcribe:en",
    "rt:gpt-transcribe:auto",
    "rt:gpt-4o-transcribe:en",
    "rt:gpt-realtime-whisper:en",
    "rt:gpt-live-transcribe:en",
]
KEYWORDS = {"waiver", "waive", "discount", "late", "fee", "minimum", "due", "outstanding", "otp",
            "callback", "settlement", "balance"}


def live_config() -> str:
    """The setup bot/main.py runs (OPENAI_STT_MODEL, language en)."""
    return f"rt:{os.getenv('OPENAI_STT_MODEL', 'gpt-transcribe')}:en"


def load_clips() -> list[dict]:
    if not STT_SET.exists():
        return []
    return [json.loads(line) for line in STT_SET.read_text().splitlines() if line.strip()]


def _load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _pcm24k(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if rate != 24000:
        t = np.arange(0, len(x) * 24000 / rate) * rate / 24000
        x = np.interp(t, np.arange(len(x)), x)
    return x.astype(np.int16).tobytes()


async def run_config(client, api_key: str, config: str, clip: dict) -> dict:
    kind, model, mode = config.split(":")
    path = REPO_ROOT / clip["clip"]
    if kind == "old":
        f = io.BytesIO(path.read_bytes())
        f.name = "clip.wav"
        r = await client.audio.transcriptions.create(model=model, file=f, language="en", prompt=OLD_HINT)
        return {"text": r.text.strip(), "confidence": None}
    from evals.realtime_stt import transcribe
    return await transcribe(_pcm24k(path), api_key, model=model,
                            language=None if mode == "auto" else mode, noise_reduction="far_field")


async def fill_cache(client, api_key: str, configs: list[str], clips: list[dict]) -> dict:
    cache = _load_cache()
    sem = asyncio.Semaphore(6)

    async def one(config, clip):
        key = f"{config}|{clip['clip']}"
        if key in cache:
            return
        async with sem:
            try:
                cache[key] = await run_config(client, api_key, config, clip)
            except Exception as e:  # a model the key can't reach, a timeout: score what ran
                cache[key] = {"text": "", "confidence": None, "error": str(e)[:200]}

    await asyncio.gather(*(one(c, clip) for c in configs for clip in clips))
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return cache


def _wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[len(hyp)] / max(1, len(ref))


def outputs_for(config: str, clips: list[dict], cache: dict) -> list[tuple[dict, dict]]:
    """(clip, {text, confidence}) pairs. The live setup's own results, saved
    during the call, are used when the cache has none."""
    pairs = []
    for clip in clips:
        out = cache.get(f"{config}|{clip['clip']}")
        if out is None and config == live_config() and clip.get("live"):
            out = clip["live"]
        if out is not None and not out.get("error"):
            pairs.append((clip, out))
    return pairs


def score(pairs: list[tuple[dict, dict]], filter_config: dict | None = None) -> dict:
    filter_config = filter_config or stt_filter.load_config()
    noise = [(c, o) for c, o in pairs if c["label"] == "noise"]
    speech = [(c, o) for c, o in pairs if c["label"] == "speech"]

    def passes(o):
        return bool(o["text"].strip()) and unclear_reason(o["text"], confidence=o.get("confidence"),
                                                         config=filter_config) is None

    wers = [_wer(words(c["reference"]), words(o["text"])) for c, o in speech]
    false_kw = sum(1 for c, o in pairs if (set(words(o["text"])) & KEYWORDS) - set(words(c["reference"])))
    return {
        "clips": len(pairs),
        "noise": len(noise),
        "speech": len(speech),
        "junk_raw": sum(1 for _, o in noise if o["text"].strip()),
        "junk_passed": sum(1 for _, o in noise if passes(o)),
        "speech_dropped": sum(1 for _, o in speech if not passes(o)),
        "wer": round(sum(wers) / len(wers), 3) if wers else None,
        "false_keywords": false_kw,
    }


def report(configs: list[str], cache: dict, clips: list[dict]) -> list[dict]:
    rows = [{"config": c, **score(outputs_for(c, clips, cache))} for c in configs]
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


def tune(apply: bool = True) -> dict | None:
    """Re-fits the filter's confidence thresholds on the live setup's
    outputs. Writes the new config only if junk_passed and speech_dropped
    both stay the same or improve and their sum improves; every accepted
    change is logged with before and after numbers. Never touches the word
    lists by hand-picked rules, the prompt, or anything the bot says."""
    clips = load_clips()
    pairs = [(c, o) for c, o in outputs_for(live_config(), clips, _load_cache()) if o.get("confidence") is not None]
    if not pairs:
        return None
    current = stt_filter.load_config()
    before = score(pairs, current)
    best, best_after = None, before
    for mc, swc in itertools.product([0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6],
                                     [0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]):
        if swc < mc:
            continue
        cand = {**current, "min_confidence": mc, "single_word_min_confidence": swc}
        after = score(pairs, cand)
        if (after["junk_passed"] <= before["junk_passed"] and after["speech_dropped"] <= before["speech_dropped"]
                and after["junk_passed"] + after["speech_dropped"]
                < best_after["junk_passed"] + best_after["speech_dropped"]):
            best, best_after = cand, after
    if best is None:
        return None
    change = {
        "at": datetime.now(timezone.utc).isoformat(),
        "config": live_config(),
        "clips": len(pairs),
        "from": {k: current[k] for k in ("min_confidence", "single_word_min_confidence")},
        "to": {k: best[k] for k in ("min_confidence", "single_word_min_confidence")},
        "before": before,
        "after": best_after,
    }
    if apply:
        saved = {k: best[k] for k in stt_filter.DEFAULT_CONFIG}
        stt_filter.CONFIG_PATH.write_text(json.dumps(saved, indent=2) + "\n")
        with TUNING_LOG.open("a") as f:
            f.write(json.dumps(change) + "\n")
    return change


async def main() -> None:
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", nargs="*", default=None, help=f"Setups to score (default: {CONFIGS})")
    parser.add_argument("--tune", action="store_true", help="Re-fit the filter thresholds afterwards")
    args = parser.parse_args()

    clips = load_clips()
    configs = args.config or CONFIGS
    key = os.getenv("OPENAI_API_KEY")
    cache = await fill_cache(AsyncOpenAI(api_key=key), key, configs, clips)
    print(f"{len(clips)} clips "
          f"({sum(c['label'] == 'speech' for c in clips)} speech, {sum(c['label'] == 'noise' for c in clips)} noise)")
    for row in report(configs, cache, clips):
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
    if args.tune:
        change = tune(apply=True)
        print(f"tuned: {change['from']} -> {change['to']}" if change else "tuning: no change improves the set")


if __name__ == "__main__":
    asyncio.run(main())
