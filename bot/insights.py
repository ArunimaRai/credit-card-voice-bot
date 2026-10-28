"""Read-only views behind the console's Guidelines, Personas and Analytics
pages. Everything here is computed from files on disk on every request, so
the pages show what the system holds right now:

- voice-guide.md, the rules the bot is built to (Guidelines);
- evals/personas.yaml + evals/live/promoted_personas.yaml, joined with the
  latest suite run in evals/results/ (Personas);
- data/calls/ (real calls), evals/results/ (simulated suite) and evals/live/
  (STT and replay sets) for the metrics (Analytics).

Nothing here writes. A missing file gives an empty section, not an error.
"""

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from bot.analysis import CALLS_DIR, EVAL_CANDIDATES, review_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
VOICE_GUIDE = REPO_ROOT / "voice-guide.md"
PERSONAS = REPO_ROOT / "evals" / "personas.yaml"
PROMOTED = REPO_ROOT / "evals" / "live" / "promoted_personas.yaml"
BY_CALL = REPO_ROOT / "evals" / "results" / "by_call.csv"
BY_ASSERTION = REPO_ROOT / "evals" / "results" / "by_assertion.csv"
STT_RESULTS = REPO_ROOT / "evals" / "live" / "stt_results.csv"
REPLAY_RESULTS = REPO_ROOT / "evals" / "live" / "replay_results.csv"
EVAL_CASES = REPO_ROOT / "data" / "eval_cases"
CUSTOMERS = json.loads((Path(__file__).parent / "fixtures" / "customers.json").read_text())

# The STT config the live pipeline runs (bot/main.py, OPENAI_STT_MODEL default, English).
LIVE_STT_CONFIG = "rt:gpt-transcribe:en"
# The bot's spoken AI disclosure (Stage 2 of voice-guide.md).
DISCLOSURE_RE = re.compile(r"automated assistant|this call is recorded", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mtime(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.exists() else None


def _csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _yaml(path: Path) -> list[dict]:
    return (yaml.safe_load(path.read_text()) or []) if path.exists() else []


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _live_calls() -> list[dict]:
    """Analysed real calls. eval-* files are the suite's own analysis runs."""
    calls = []
    for f in sorted(CALLS_DIR.glob("*.json")) if CALLS_DIR.exists() else []:
        if f.name.startswith("eval-"):
            continue
        try:
            entry = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if entry.get("status") == "ready" and entry.get("record"):
            calls.append(entry)
    return calls


def _performance(entry: dict) -> dict:
    # Calls analysed before review_agent existed have no performance block; run it now.
    if entry.get("performance"):
        return entry["performance"]
    record = entry["record"]
    return review_agent(record, CUSTOMERS.get(record.get("customer_id")))


# --- Guidelines ---------------------------------------------------------

def _sections(md: str) -> dict[str, str]:
    """Top-level '## ' sections keyed by heading without its number."""
    out = {}
    for block in re.split(r"^## ", md, flags=re.M)[1:]:
        head, _, body = block.partition("\n")
        out[re.sub(r"^\d+\.\s*", "", head).strip()] = body.strip()
    return out


def _table(body: str) -> list[list[str]]:
    rows = [r for r in body.splitlines() if r.strip().startswith("|")]
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    return [c for c in cells[1:] if not all(set(x) <= {"-", ":"} for x in c)]


def guidelines() -> dict:
    if not VOICE_GUIDE.exists():
        return {"generated_at": _now(), "source": None}
    sec = _sections(VOICE_GUIDE.read_text())

    groups, current = [], None
    for line in sec.get("Non-Negotiables", "").splitlines():
        if m := re.match(r"^\*\*(From .+)\*\*$", line.strip()):
            current = {"title": m.group(1), "rules": []}
            groups.append(current)
        elif current is not None and (m := re.match(r"^(\d+)\.\s+\*\*(.+?)\*\*\s*(.*)$", line.strip())):
            current["rules"].append({"n": int(m.group(1)), "title": m.group(2).rstrip("."), "text": m.group(3)})

    compliance = sec.get("Compliance", "")
    defaults = [re.sub(r"^- ", "", l.strip()) for l in compliance.split("**How each rule")[0].splitlines()
                if l.strip().startswith("- ")]

    # How often each code check fired on real calls, so the page shows the rule working.
    fired = Counter(t.get("rule") for e in _live_calls() for t in e["record"].get("guardrail_triggers") or [])
    enforcement = []
    for rule, by in _table(compliance):
        checks = re.findall(r"`(no_[a-z_]+)`", by)
        enforcement.append({"rule": rule, "enforced_by": by, "checks": checks,
                            "fired_live": sum(fired.get(c, 0) for c in checks)})

    words = sec.get("Words and Phrases", "")
    prefer = _table(words.split("### Banned")[0])
    banned_block = words.split("### Banned outright")[-1] if "### Banned outright" in words else ""
    banned = re.findall(r"\"([^\"]+)\"", banned_block.split("\n\n")[1] if "\n\n" in banned_block else banned_block)

    structure = sec.get("Call Structure", "")
    stages = []
    for t, b in re.findall(r"^### (.+?)\n(.*?)(?=^### |\Z)", structure, flags=re.M | re.S):
        goal = re.search(r"^\*\*Goal:\*\*\s*(.+)$", b, flags=re.M)
        sample = re.search(r"^>\s*\"(.+?)\"", b, flags=re.M)
        # No **Goal:** line: use the stage's first plain sentence, unless it only introduces a sample.
        lead = next((p.strip() for p in b.split("\n\n") if p.strip() and not p.lstrip().startswith((">", "-", "|", "**"))), "")
        stages.append({"title": t.strip(), "goal": goal.group(1).replace("_", "") if goal else ("" if lead.endswith(":") else lead),
                       "sample": sample.group(1) if sample else None})

    return {
        "generated_at": _now(),
        "source": {"path": "voice-guide.md", "modified_at": _mtime(VOICE_GUIDE)},
        "non_negotiables": groups,
        "stages": stages,
        "compliance_defaults": defaults,
        "enforcement": enforcement,
        "prefer": [{"say": r[0], "avoid": r[1]} for r in prefer if len(r) >= 2],
        "banned": banned,
        "live_calls": len(_live_calls()),
    }


# --- Personas -----------------------------------------------------------

def _expected(p: dict) -> list[str]:
    o = (p.get("assert") or {}).get("outcome")
    return o if isinstance(o, list) else [o] if o else []


def personas() -> dict:
    core, promoted = _yaml(PERSONAS), _yaml(PROMOTED)
    runs = {r["persona_id"]: r for r in _csv(BY_CALL)}
    fails = Counter(r["persona_id"] for r in _csv(BY_ASSERTION) if r.get("passed") == "False")
    checks = Counter(r["persona_id"] for r in _csv(BY_ASSERTION))

    rows = []
    for p in [*core, *promoted]:
        run = runs.get(p["id"])
        rows.append({
            "id": p["id"],
            "archetype": p.get("archetype", ""),
            "difficulty": p.get("difficulty", ""),
            "source": {"B": "brief", "I": "inferred", "L": "live"}.get(p.get("src"), p.get("src")),
            "brief": " ".join((p.get("customer_brief") or "").split()),
            "expected": _expected(p),
            "must_not_say": (p.get("assert") or {}).get("must_not_say") or [],
            "from_call": p.get("from_call"),
            "result": run and {
                "version": run["version"],
                "outcome": run["outcome_type"],
                "critical": int(run["n_critical"]),
                "major": int(run["n_major"]),
                "minor": int(run["n_minor"]),
                "score": float(run["weighted_score"]),
                "checks": checks.get(p["id"], 0),
                "failed": fails.get(p["id"], 0),
                "duration_sec": float(run["duration_sec"]),
            },
        })

    cases = list(EVAL_CASES.glob("*.json")) if EVAL_CASES.exists() else []
    candidates = EVAL_CANDIDATES.read_text().splitlines() if EVAL_CANDIDATES.exists() else []
    return {
        "generated_at": _now(),
        "personas": rows,
        "counts": {
            "total": len(rows),
            "core": len(core),
            "learned": len(promoted),
            "by_difficulty": Counter(r["difficulty"] for r in rows),
        },
        "learning": {
            "live_calls": len(_live_calls()),
            "calls_turned_into_cases": len(cases),
            "eval_candidates": len([c for c in candidates if c.strip()]),
            "last_promoted_at": _mtime(PROMOTED),
            "last_suite_run_at": _mtime(BY_CALL),
        },
    }


# --- Analytics ----------------------------------------------------------

def analytics() -> dict:
    calls = _live_calls()
    records = [e["record"] for e in calls]
    engaged = [e for e in calls if any(t["role"] == "customer" for t in e["record"].get("transcript", []))]
    outcomes = Counter(e["record"]["outcome"]["type"] for e in calls if e["record"].get("outcome"))
    resolved = [e for e in engaged if e["record"]["outcome"]["type"] != "no_outcome"]

    bot_turns = [t for r in records for t in r.get("transcript", []) if t["role"] == "bot"]
    cust_turns = [t for r in records for t in r.get("transcript", []) if t["role"] == "customer"]
    unclear = [t for t in cust_turns if t.get("unclear")]
    verified = [r for r in records if r.get("identity_confirmed")]
    disclosed = [r for r in verified if any(t["role"] == "bot" and DISCLOSURE_RE.search(t["text"])
                                            for t in r.get("transcript", []))]
    durations = [r["duration_sec"] for r in records if r.get("duration_sec")]

    findings = [f for e in calls for f in _performance(e).get("findings", [])]
    sev = Counter(f["severity"] for f in findings)
    by_key = Counter((f["key"], f["title"], f["severity"]) for f in findings)
    triggers = Counter(t.get("rule") for r in records for t in r.get("guardrail_triggers") or [])

    sent = [e.get("sentiment") or {} for e in engaged]
    sent_end = Counter(s.get("end") for s in sent if s.get("end"))
    sent_moves = Counter(f"{s.get('start')}→{s.get('end')}" for s in sent if s.get("start"))

    # Simulated suite: the latest run of every persona.
    runs, asserts = _csv(BY_CALL), _csv(BY_ASSERTION)
    buckets: dict[str, dict] = {}
    for a in asserts:
        if a.get("passed") not in ("True", "False"):
            continue  # rows split by unquoted commas in old runs
        b = buckets.setdefault(a["bucket"], {"bucket": a["bucket"], "checks": 0, "passed": 0, "critical_failed": 0})
        b["checks"] += 1
        b["passed"] += a["passed"] == "True"
        b["critical_failed"] += a["passed"] == "False" and a["severity"] == "critical"
    transfer_needed = [r for r in runs if r["transfer_required"] == "True"]
    transfer_ok = [r for r in runs if r["transfer_required"] == r["transfer_happened"]]

    stt = _csv(STT_RESULTS)
    live_stt = next((r for r in stt if r["config"] == LIVE_STT_CONFIG), None)
    replay = _csv(REPLAY_RESULTS)

    return {
        "generated_at": _now(),
        "live": {
            "calls": len(calls),
            "engaged": len(engaged),
            "last_call_at": max((r.get("started_at") or "" for r in records), default=None) or None,
            "resolution_rate": _rate(len(resolved), len(engaged)),
            "outcomes": outcomes,
            "promise_to_pay_rate": _rate(outcomes.get("promise_to_pay", 0), len(engaged)),
            "transfer_rate": _rate(outcomes.get("dispute_transfer", 0), len(engaged)),
            "right_party_rate": _rate(len(verified), len(engaged)),
            "disclosure_rate": _rate(len(disclosed), len(verified)),
            "avg_duration_sec": round(sum(durations) / len(durations), 1) if durations else None,
            "avg_turns": round((len(bot_turns) + len(cust_turns)) / len(calls), 1) if calls else None,
            "avg_bot_words": round(sum(len(t["text"].split()) for t in bot_turns) / len(bot_turns), 1) if bot_turns else None,
            "unclear_rate": _rate(len(unclear), len(cust_turns)),
            "customer_turns": len(cust_turns),
            "guardrail_triggers": triggers,
            "findings": {"total": len(findings), "critical": sev.get("critical", 0),
                         "major": sev.get("major", 0), "minor": sev.get("minor", 0),
                         "clean_calls": sum(1 for e in engaged if not _performance(e).get("findings"))},
            "top_findings": [{"key": k, "title": t, "severity": s, "count": n}
                             for (k, t, s), n in by_key.most_common(8)],
            "sentiment_end": sent_end,
            "sentiment_moves": sent_moves,
        },
        "suite": {
            "runs": len(runs),
            "last_run_at": _mtime(BY_CALL),
            "version": runs[-1]["version"] if runs else None,
            "outcome_valid_rate": _rate(sum(r["outcome_valid"] == "True" for r in runs), len(runs)),
            "transfer_accuracy": _rate(len(transfer_ok), len(runs)),
            "transfers_required": len(transfer_needed),
            "clean_runs": sum(1 for r in runs if r["n_critical"] == r["n_major"] == r["n_minor"] == "0"),
            "critical": sum(int(r["n_critical"]) for r in runs),
            "major": sum(int(r["n_major"]) for r in runs),
            "minor": sum(int(r["n_minor"]) for r in runs),
            "avg_score": round(sum(float(r["weighted_score"]) for r in runs) / len(runs), 3) if runs else None,
            "buckets": sorted(buckets.values(), key=lambda b: b["bucket"]),
        },
        "speech": {
            "live_config": LIVE_STT_CONFIG,
            "clips": int(live_stt["clips"]) if live_stt else None,
            "wer": float(live_stt["wer"]) if live_stt else None,
            "junk_passed": int(live_stt["junk_passed"]) if live_stt else None,
            "noise": int(live_stt["noise"]) if live_stt else None,
            "speech_dropped": int(live_stt["speech_dropped"]) if live_stt else None,
            "speech": int(live_stt["speech"]) if live_stt else None,
            "configs": [{"config": r["config"], "wer": float(r["wer"]), "junk_passed": int(r["junk_passed"]),
                         "speech_dropped": int(r["speech_dropped"])} for r in stt],
            "replay_cases": len(replay),
            "replay_passed": sum(r["passed"] == "True" for r in replay),
        },
    }
