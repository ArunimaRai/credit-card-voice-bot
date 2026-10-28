#!/usr/bin/env python3
"""Sample ~20% of judge-scored rows for blind human review, then compute
judge-human agreement once a human fills them in (Execution.md §10: "Hand-
score ~20% of transcripts... report judge-human agreement... if agreement
is under ~80%, the rubric is the problem, not the judge.").

Deliberately excludes the judge's own verdict from the sample file — the
scoring is supposed to be blind. Open the matching transcript under
evals/results/transcripts/<call_id>.json to score each row.

Usage:
    uv run evals/hand_score.py                  # write the sample to fill in
    uv run evals/hand_score.py --agreement       # after filling human_passed
"""

import argparse
import csv
import random
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
BY_ASSERTION = RESULTS_DIR / "by_assertion.csv"
SAMPLE = RESULTS_DIR / "hand_score_sample.csv"


def sample(fraction: float, seed: int) -> None:
    with BY_ASSERTION.open() as f:
        judge_rows = [r for r in csv.DictReader(f) if r["bucket"] == "judge"]
    if not judge_rows:
        print(f"No judge-bucket rows in {BY_ASSERTION} — run evals/harness.py first.")
        return

    call_ids = sorted({r["call_id"] for r in judge_rows})
    random.Random(seed).shuffle(call_ids)
    n = max(1, round(len(call_ids) * fraction))
    chosen = set(call_ids[:n])
    sampled = [r for r in judge_rows if r["call_id"] in chosen]

    with SAMPLE.open("w", newline="") as f:
        fieldnames = [
            "call_id", "persona_id", "archetype", "version",
            "assertion_id", "expected", "human_passed", "human_notes",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sampled:
            writer.writerow(
                {
                    "call_id": row["call_id"],
                    "persona_id": row["persona_id"],
                    "archetype": row["archetype"],
                    "version": row["version"],
                    "assertion_id": row["assertion_id"],
                    "expected": row["expected"],
                    "human_passed": "",
                    "human_notes": "",
                }
            )

    print(f"Sampled {len(chosen)}/{len(call_ids)} calls ({len(sampled)} judge assertions) -> {SAMPLE}")
    print(
        "Read each call's transcript under evals/results/transcripts/<call_id>.json, then fill "
        "human_passed with true/false — blind to the judge's own verdict, which is intentionally "
        "left out of this file. Then run: uv run evals/hand_score.py --agreement"
    )


def agreement() -> None:
    with BY_ASSERTION.open() as f:
        judge_passed = {(r["call_id"], r["assertion_id"]): r["passed"] for r in csv.DictReader(f)}
    with SAMPLE.open() as f:
        human_rows = list(csv.DictReader(f))

    scored = [r for r in human_rows if r["human_passed"].strip() != ""]
    if not scored:
        print(f"No human_passed values filled in yet — edit {SAMPLE} first.")
        return

    matches = 0
    disagreements = []
    for row in scored:
        key = (row["call_id"], row["assertion_id"])
        judge_val = judge_passed.get(key)
        if judge_val is None:
            continue
        human_val = row["human_passed"].strip().lower() in ("true", "1", "yes", "pass")
        if (judge_val.strip().lower() == "true") == human_val:
            matches += 1
        else:
            disagreements.append(row["call_id"] + ":" + row["assertion_id"])

    rate = matches / len(scored)
    print(f"Judge-human agreement: {matches}/{len(scored)} = {rate:.1%}")
    if disagreements:
        print("Disagreements:", ", ".join(disagreements))
    if rate < 0.8:
        print(
            "Below ~80% — per Execution.md §10, treat the rubric as the problem, not the judge. "
            "Tighten judge.py's rubric text and rerun."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--agreement", action="store_true")
    args = parser.parse_args()

    if args.agreement:
        agreement()
    else:
        sample(args.fraction, args.seed)
