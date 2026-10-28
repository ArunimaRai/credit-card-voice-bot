#!/usr/bin/env python3
"""Eval harness (Execution.md §10, D2). Drives two LLMs against each other
and scores the transcript: a simulated customer (built from each persona's
`customer_brief` in evals/personas.yaml) against the bot's real prompt,
tools, and model — imported from bot/, never reimplemented, so a result is
a result about the shipped system, not a copy of it.

This talks to the OpenAI Chat Completions API directly rather than booting
bot/main.py's Pipecat pipeline, because that pipeline is wired end to end
for audio frames (WebRTC/STT/TTS) with no text-mode entry point — faking
audio to reuse it would test the transport, not the conversation. What *is*
shared, verbatim, is everything that makes the conversation the
conversation: bot.prompting's system/developer-fact construction and
bot.tools' outcome-tool schema per version.

This harness never POSTs to WEBHOOK_URL. The webhook.site bin is for real
demo calls (and has a one-week expiry, PROGRESS.md) — synthetic eval traffic
is scored locally and written to evals/results/ instead.

Usage:
    uv run evals/harness.py --version v1
    uv run evals/harness.py --version v1 --persona P01 P06
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bot import guardrails  # noqa: E402
from bot.prompting import build_developer_message, build_system_prompt  # noqa: E402
from bot.schema import validate_outcome  # noqa: E402
from bot.state import CallState  # noqa: E402
from bot.tools import TOOLS_BY_VERSION  # noqa: E402
from evals import assertions, judge  # noqa: E402

load_dotenv(REPO_ROOT / ".env")  # does not override already-exported shell vars

RESULTS_DIR = REPO_ROOT / "evals" / "results"
TRANSCRIPTS_DIR = RESULTS_DIR / "transcripts"

BOT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
SIM_MODEL = os.getenv("SIM_CUSTOMER_MODEL", "gpt-4.1-mini")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4o")  # deliberately a different model than the bot's own
MAX_TURNS = int(os.getenv("EVAL_MAX_TURNS", "14"))

CUSTOMERS = json.loads((REPO_ROOT / "bot" / "fixtures" / "customers.json").read_text())
DEFAULT_CUSTOMER_ID = "MB-0007"

SIM_SYSTEM_TEMPLATE = """You are roleplaying as a bank customer on a phone call, for a \
collections-bot quality test. Stay in character as the customer only — never mention being \
an AI, a test, or a persona, and never break character.

Your situation: {brief}

Rules: speak naturally, one short turn at a time, the way a real person talks on the phone \
(no stage directions, no narration, no quotation marks around your line). Never volunteer \
information the brief above didn't give you. React in character to whatever the agent says."""


@dataclass
class CallResult:
    call_id: str
    persona: dict
    version: str
    customer: dict
    transcript: list[dict] = field(default_factory=list)
    outcome: dict = field(default_factory=dict)
    duration_sec: float = 0.0


def load_personas(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


async def simulate_customer_turn(client: AsyncOpenAI, history: list[dict], brief: str) -> str:
    messages = [{"role": "system", "content": SIM_SYSTEM_TEMPLATE.format(brief=brief)}] + history
    response = await client.chat.completions.create(model=SIM_MODEL, messages=messages, temperature=0.8)
    return (response.choices[0].message.content or "").strip()


async def run_bot_turn(client: AsyncOpenAI, version: str, messages: list[dict]) -> tuple[str | None, dict | None]:
    """One bot turn. Mutates `messages` in place with whatever the model
    produced (assistant text, or a tool call plus its synthetic result),
    mirroring the production context aggregator."""
    tool_def = TOOLS_BY_VERSION[version][0]
    response = await client.chat.completions.create(
        model=BOT_MODEL, messages=messages, tools=[tool_def], tool_choice="auto"
    )
    choice = response.choices[0].message
    outcome = None

    if choice.tool_calls:
        call = choice.tool_calls[0]
        try:
            outcome = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            outcome = {"type": "no_outcome", "reason": "unparseable tool arguments"}
        messages.append(
            {
                "role": "assistant",
                "content": choice.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"status": "recorded"})})
    else:
        messages.append({"role": "assistant", "content": choice.content or ""})

    return choice.content, outcome


async def _generate_candidate(client: AsyncOpenAI, messages: list[dict], tool_def: dict):
    response = await client.chat.completions.create(
        model=BOT_MODEL, messages=messages, tools=[tool_def], tool_choice="auto"
    )
    return response.choices[0].message


async def _guardrail_check(client: AsyncOpenAI, text: str, state: CallState, customer: dict):
    result = guardrails.check_deterministic(
        text,
        otp_verified=state.otp_verified,
        identity_confirmed=state.identity_confirmed,
        min_due=customer["min_due"],
    )
    if result.triggered:
        return result
    return await guardrails.llm_check(client, JUDGE_MODEL, text)


async def run_bot_turn_v3(
    client: AsyncOpenAI, messages: list[dict], state: CallState, customer: dict
) -> tuple[str | None, dict | None]:
    """v3 only: wraps the raw model turn with the L4 guardrail pass on
    candidate text (Execution.md §7) and schema + business-rule validation
    on any outcome tool call, rejecting and re-asking rather than accepting
    blindly (§6, §8). v1/v2 keep using the plain run_bot_turn above, so the
    v2->v3 table isolates what this structure buys over wording alone."""
    tool_def = TOOLS_BY_VERSION["v3"][0]

    async def handle_tool_call(choice, retries_left: int) -> dict:
        call = choice.tool_calls[0]
        try:
            args = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            args = {"type": "no_outcome", "reason": "unparseable tool arguments"}
        error = validate_outcome(args, customer)
        messages.append(
            {
                "role": "assistant",
                "content": choice.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                ],
            }
        )
        if error is None:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"status": "recorded"})})
            return args
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": json.dumps({"status": "rejected", "reason": error})}
        )
        if retries_left <= 0:
            return {"type": "no_outcome", "reason": f"outcome rejected and never corrected: {error}"}
        messages.append(
            {
                "role": "developer",
                "content": f"That outcome was rejected: {error}. Call record_outcome again with a corrected, valid outcome.",
            }
        )
        retry_choice = await _generate_candidate(client, messages, tool_def)
        if retry_choice.tool_calls:
            return await handle_tool_call(retry_choice, retries_left - 1)
        messages.append({"role": "assistant", "content": retry_choice.content or ""})
        return {"type": "no_outcome", "reason": "model did not correct the rejected outcome"}

    choice = await _generate_candidate(client, messages, tool_def)
    if choice.tool_calls:
        outcome = await handle_tool_call(choice, retries_left=2)
        return choice.content, outcome

    text = choice.content or ""
    if not text:
        messages.append({"role": "assistant", "content": text})
        return text, None

    result = await _guardrail_check(client, text, state, customer)
    if not result.triggered:
        messages.append({"role": "assistant", "content": text})
        return text, None

    # Block, regenerate once (Execution.md §7), then fall back to the canned
    # safe line — an unbounded retry loop is a worse failure than a bland
    # sentence.
    messages.append({"role": "assistant", "content": text})
    messages.append(
        {
            "role": "developer",
            "content": f"That reply violated policy ({result.rule}). Say something that does not "
            "repeat the violation, and continue the call.",
        }
    )
    retry_choice = await _generate_candidate(client, messages, tool_def)
    messages.pop()  # remove the nudge
    messages.pop()  # remove the rejected candidate

    if retry_choice.tool_calls:
        outcome = await handle_tool_call(retry_choice, retries_left=2)
        return retry_choice.content, outcome

    retry_text = retry_choice.content or ""
    retry_result = await _guardrail_check(client, retry_text, state, customer) if retry_text else result
    final_text = retry_text if not retry_result.triggered else result.safe_text
    messages.append({"role": "assistant", "content": final_text})
    return final_text, None


async def run_call(
    client: AsyncOpenAI, persona: dict, version: str, customer_id: str = DEFAULT_CUSTOMER_ID
) -> CallResult:
    customer = CUSTOMERS[customer_id]
    system_prompt = build_system_prompt(version, customer)
    dev_message = build_developer_message(version, customer)
    system_text = system_prompt if not dev_message else f"{system_prompt}\n\n{dev_message}"

    bot_messages = [
        {"role": "system", "content": system_text},
        {"role": "system", "content": "The call has connected. Begin the call now."},
    ]
    transcript: list[dict] = []
    sim_history: list[dict] = []
    started = time.time()
    state = CallState() if version == "v3" else None

    async def bot_turn() -> tuple[str | None, dict | None]:
        if state is not None:
            return await run_bot_turn_v3(client, bot_messages, state, customer)
        return await run_bot_turn(client, version, bot_messages)

    bot_text, outcome = await bot_turn()
    if bot_text:
        transcript.append({"role": "bot", "text": bot_text})
        sim_history.append({"role": "user", "content": bot_text})

    customer_brief = persona["customer_brief"].strip()
    turns = 0
    while outcome is None and turns < MAX_TURNS:
        turns += 1
        customer_text = await simulate_customer_turn(client, sim_history, customer_brief)
        transcript.append({"role": "customer", "text": customer_text})
        sim_history.append({"role": "assistant", "content": customer_text})
        bot_messages.append({"role": "user", "content": customer_text})

        if state is not None:
            state.note_turn()
            state.observe_customer_turn(customer_text, expected_otp=customer.get("otp"))
            for nudge in state.pending_nudges():
                bot_messages.append({"role": "developer", "content": nudge})

        bot_text, outcome = await bot_turn()
        if bot_text:
            transcript.append({"role": "bot", "text": bot_text})
            sim_history.append({"role": "user", "content": bot_text})

    if outcome is None:
        # Mirrors bot/main.py's disconnect safety net: a call must be filed
        # as *something*, never silently dropped.
        outcome = {"type": "no_outcome", "reason": "max turns reached without a recorded outcome"}

    call_id = f"{persona['id']}-{version}-{int(started * 1000) % 1_000_000}"
    return CallResult(
        call_id=call_id,
        persona=persona,
        version=version,
        customer=customer,
        transcript=transcript,
        outcome=outcome,
        duration_sec=round(time.time() - started, 1),
    )


def write_transcript(result: CallResult) -> None:
    redacted = [
        {"role": t["role"], "text": assertions.redact_pii(t["text"])} for t in result.transcript
    ]
    payload = {
        "call_id": result.call_id,
        "persona_id": result.persona["id"],
        "archetype": result.persona["archetype"],
        "version": result.version,
        "outcome": result.outcome,
        "duration_sec": result.duration_sec,
        "transcript": redacted,
    }
    (TRANSCRIPTS_DIR / f"{result.call_id}.json").write_text(json.dumps(payload, indent=2))


def write_csvs(by_assertion_rows: list[dict], by_call_rows: list[dict]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with (RESULTS_DIR / "by_assertion.csv").open("w", newline="") as f:
        fieldnames = [
            "call_id", "persona_id", "archetype", "src", "version",
            "assertion_id", "bucket", "severity", "expected", "actual", "passed",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(by_assertion_rows)

    with (RESULTS_DIR / "by_call.csv").open("w", newline="") as f:
        fieldnames = [
            "call_id", "persona_id", "difficulty", "version", "outcome_type", "outcome_valid",
            "signals", "transfer_required", "transfer_happened", "n_critical", "n_major",
            "n_minor", "weighted_score", "duration_sec", "judge_notes",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(by_call_rows)


def summarize(by_assertion_rows: list[dict], by_call_rows: list[dict]) -> dict:
    total_weight = sum(assertions.WEIGHTS[r["severity"]] for r in by_assertion_rows)
    failed_weight = sum(assertions.WEIGHTS[r["severity"]] for r in by_assertion_rows if not r["passed"])
    n_assertions = len(by_assertion_rows)
    n_passed = sum(1 for r in by_assertion_rows if r["passed"])
    n_calls = len(by_call_rows)
    n_critical_calls = sum(1 for r in by_call_rows if int(r["n_critical"]) > 0)
    n_clean_calls = sum(1 for r in by_call_rows if int(r["n_critical"]) == 0 and int(r["n_major"]) == 0)

    per_bucket: dict[str, dict] = {}
    for row in by_assertion_rows:
        bucket = row["bucket"]
        b = per_bucket.setdefault(bucket, {"total": 0, "failed": 0})
        b["total"] += 1
        if not row["passed"]:
            b["failed"] += 1

    return {
        "weighted_failure_rate": failed_weight / total_weight if total_weight else 0.0,
        "assertion_pass_rate": n_passed / n_assertions if n_assertions else 0.0,
        "critical_violation_rate": n_critical_calls / n_calls if n_calls else 0.0,
        "call_pass_rate": n_clean_calls / n_calls if n_calls else 0.0,
        "per_bucket_failure_rate": {
            bucket: b["failed"] / b["total"] for bucket, b in per_bucket.items()
        },
    }


async def run_version(
    client: AsyncOpenAI, personas: list[dict], version: str, concurrency: int
) -> tuple[list[dict], list[dict]]:
    """Runs one version over all given personas. Returns (by_assertion_rows,
    by_call_rows) for just this version -- callers combine across versions."""
    sem = asyncio.Semaphore(concurrency)

    async def bound(persona: dict) -> CallResult:
        async with sem:
            return await run_call(client, persona, version)

    results = await asyncio.gather(*(bound(p) for p in personas))

    by_assertion_rows: list[dict] = []
    by_call_rows: list[dict] = []

    for result in results:
        write_transcript(result)
        det_rows = assertions.evaluate(result)
        judge_rows, judge_notes = await judge.evaluate(client, JUDGE_MODEL, result)
        all_rows = det_rows + judge_rows
        by_assertion_rows.extend(all_rows)

        n_critical = sum(1 for r in all_rows if not r["passed"] and r["severity"] == "critical")
        n_major = sum(1 for r in all_rows if not r["passed"] and r["severity"] == "major")
        n_minor = sum(1 for r in all_rows if not r["passed"] and r["severity"] == "minor")
        total_w = sum(assertions.WEIGHTS[r["severity"]] for r in all_rows)
        failed_w = sum(assertions.WEIGHTS[r["severity"]] for r in all_rows if not r["passed"])
        signal_rows = [r for r in all_rows if r["assertion_id"] == "judge:signals"]

        by_call_rows.append(
            {
                "call_id": result.call_id,
                "persona_id": result.persona["id"],
                "difficulty": result.persona["difficulty"],
                "version": result.version,
                "outcome_type": result.outcome.get("type"),
                "outcome_valid": all(
                    r["passed"] for r in all_rows if r["assertion_id"].startswith("outcome")
                ),
                "signals": signal_rows[0]["passed"] if signal_rows else "",
                "transfer_required": result.persona["assert"].get("transfer_required", False),
                "transfer_happened": result.outcome.get("type") == "dispute_transfer",
                "n_critical": n_critical,
                "n_major": n_major,
                "n_minor": n_minor,
                "weighted_score": round(failed_w / total_w, 3) if total_w else 0.0,
                "duration_sec": result.duration_sec,
                "judge_notes": judge_notes,
            }
        )
        print(f"  {result.call_id}: outcome={result.outcome.get('type')} "
              f"critical={n_critical} major={n_major} minor={n_minor}")

    return by_assertion_rows, by_call_rows


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", nargs="+", default=["v1"], choices=["v1", "v2", "v3"])
    parser.add_argument("--persona", nargs="*", default=None, help="Run only these persona ids")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    personas = load_personas(REPO_ROOT / "evals" / "personas.yaml")
    if args.persona:
        wanted = set(args.persona)
        personas = [p for p in personas if p["id"] in wanted]

    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    all_assertion_rows: list[dict] = []
    all_call_rows: list[dict] = []

    # One version at a time (not gathered together) so a slow/failed version
    # doesn't block or lose the others, and the per-version summary prints
    # in a sane order.
    for version in args.version:
        print(f"\n=== running {version} over {len(personas)} personas ===")
        assertion_rows, call_rows = await run_version(client, personas, version, args.concurrency)
        all_assertion_rows.extend(assertion_rows)
        all_call_rows.extend(call_rows)

        summary = summarize(assertion_rows, call_rows)
        print(f"\n{version} over {len(call_rows)} calls:")
        print(f"  weighted_failure_rate   = {summary['weighted_failure_rate']:.1%}")
        print(f"  assertion_pass_rate     = {summary['assertion_pass_rate']:.1%}")
        print(f"  critical_violation_rate = {summary['critical_violation_rate']:.1%}")
        print(f"  call_pass_rate          = {summary['call_pass_rate']:.1%}")
        print("  per-bucket failure rate:")
        for bucket, rate in sorted(summary["per_bucket_failure_rate"].items()):
            print(f"    {bucket:<14} {rate:.1%}")

    # write_csvs always overwrites -- run every version in one invocation
    # (`--version v1 v2 v3`) to get one combined, comparable CSV pair. The
    # CSV schema's own `version` column is exactly for this comparison.
    write_csvs(all_assertion_rows, all_call_rows)
    print(f"\nWrote {RESULTS_DIR / 'by_assertion.csv'} ({len(all_assertion_rows)} rows, {len(args.version)} version(s))")
    print(f"Wrote {RESULTS_DIR / 'by_call.csv'} ({len(all_call_rows)} rows)")
    print(f"Wrote {len(all_call_rows)} transcripts to {TRANSCRIPTS_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
