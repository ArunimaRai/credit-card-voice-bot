"""Replays real calls against the shipped bot (Execution Plan §16.6).

Each row of evals/live/replay_cases.jsonl is the customer side of a real call
exactly as STT heard it, junk included ("context:", "waiver.", "minimum_due").
It is fed turn by turn through the same turn gate (bot/turn_gate.py) and LLM
turn (evals/harness.py::run_bot_turn) the live call goes through, and the
result is checked with the post-call analysis's own agent checks. A replay
passes when no critical or major finding remains. That keeps a live failure
fixed once it has been fixed. The simulated-customer harness can't do this,
because it never produces STT noise.

    uv run python -m evals.replay
    uv run python -m evals.replay --call 2f9ae2f8

Writes evals/live/replay_results.csv.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from openai import AsyncOpenAI  # noqa: E402

from bot import turn_gate  # noqa: E402
from bot.analysis import review_agent  # noqa: E402
from bot.prompting import build_developer_message, build_system_prompt, opening_line  # noqa: E402
from bot.state import CallState  # noqa: E402
from evals.harness import CUSTOMERS, run_bot_turn  # noqa: E402
from evals.live_cases import LIVE_DIR, REPLAY_SET  # noqa: E402

RESULTS = LIVE_DIR / "replay_results.csv"
# A replay stops where the real customer stopped, so a missing outcome is
# expected, and STT-side findings are scored by evals/stt_eval.py instead.
IGNORED = {"no_outcome", "stt_hallucination"}


async def replay(client: AsyncOpenAI, case: dict) -> dict:
    customer = CUSTOMERS[case["customer_id"]]
    state = CallState()
    opening = opening_line(customer)
    messages = [
        {"role": "system", "content": f"{build_system_prompt(customer)}\n\n{build_developer_message(customer)}"},
        {"role": "assistant", "content": opening},
    ]
    transcript = [{"role": "bot", "text": opening}]
    outcome = None

    for turn in case["turns"]:
        gate = turn_gate.gate_turn(state, turn["text"], customer, confidence=turn.get("confidence"))
        entry = {"role": "customer", "text": turn["text"]}
        if gate and gate.unclear_reason:
            entry["unclear"] = gate.unclear_reason
        transcript.append(entry)
        if gate:
            if not gate.unclear_reason:
                messages.append({"role": "user", "content": turn["text"]})
            messages.append({"role": "assistant", "content": gate.line})
            transcript.append({"role": "bot", "text": gate.line})
            if gate.outcome:
                outcome = gate.outcome
            if gate.end_call:
                break
            continue
        for nudge in state.pending_nudges():
            messages.append({"role": "developer", "content": nudge})
        messages.append({"role": "user", "content": turn["text"]})
        text, outcome = await run_bot_turn(client, messages, state, customer)
        if text:
            transcript.append({"role": "bot", "text": text})
        if outcome:
            break

    record = {
        "outcome": outcome or {"type": "no_outcome", "reason": "replay ended"},
        "identity_confirmed": state.identity_confirmed,
        "identity_denied": state.identity_denied,
        "guardrail_triggers": [],
        "transcript": transcript,
    }
    findings = [f for f in review_agent(record, customer)["findings"] if f["key"] not in IGNORED]
    bad = [f for f in findings if f["severity"] in ("critical", "major")]
    return {
        "call_id": case["call_id"],
        "passed": not bad,
        "outcome": record["outcome"]["type"],
        "live_findings": " ".join(case.get("live_findings", [])),
        "replay_findings": " ".join(f"{f['key']}@{f['turn']}" for f in findings),
        "transcript": " | ".join(
            f"{'BOT' if t['role'] == 'bot' else 'CUST'}{'(unclear)' if t.get('unclear') else ''}: {t['text']}"
            for t in transcript
        ),
    }


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--call", nargs="*", default=None, help="Only these call ids (prefixes work)")
    args = parser.parse_args()
    cases = [json.loads(line) for line in REPLAY_SET.read_text().splitlines() if line.strip()]
    if args.call:
        cases = [c for c in cases if any(c["call_id"].startswith(p) for p in args.call)]
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    rows = await asyncio.gather(*(replay(client, c) for c in cases))
    with RESULTS.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['call_id'][:8]} outcome={r['outcome']} "
              f"live=[{r['live_findings']}] replay=[{r['replay_findings']}]\n    {r['transcript']}")
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} replays pass. Wrote {RESULTS}")


if __name__ == "__main__":
    asyncio.run(main())
