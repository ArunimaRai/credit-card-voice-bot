"""Post-call analysis: what the console shows once a call ends.

Two halves, deliberately split by who is allowed to decide what:

- Business facts — outcome, follow-up date, pipeline stage, routing team,
  flags — are derived in code from the *validated* record_outcome plus the
  L3 state machine's signals (`derive_workflow`). The model never picks a
  date, an amount or a stage, so the analysis can't invent a commitment the
  customer didn't make.
- Judgement — customer sentiment (start, end, overall) and a short summary —
  comes from one structured-output LLM call on a different model than the
  bot's own (`analyze_sentiment`), the same "don't let it grade itself"
  choice as evals/judge.py. Every evidence quote it returns is checked
  against the transcript and dropped if the customer never said it.
- Agent performance — what the bot itself got wrong on this call — is a set
  of code-only checks (`review_agent`). Each finding is also appended to
  EVAL_CANDIDATES so it can be turned into an eval case, and the next
  prompt version is graded against it.

No Pipecat dependency, so the live pipeline (bot/main.py) and offline runs
over eval transcripts share the same code.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

ANALYSIS_MODEL = "gpt-4o"

CALLS_DIR = Path(__file__).resolve().parent.parent / "data" / "calls"
EVAL_CANDIDATES = CALLS_DIR.parent / "eval_candidates.jsonl"

# key -> (label, team that owns the account next)
STAGES = {
    "wrong_party": ("Unreached / wrong party", "Collections dialer"),
    "identity_unverified": ("Identity unverified", "Collections dialer"),
    "contacted_no_commitment": ("Contacted, no commitment", "Collections dialer"),
    "callback_scheduled": ("Callback scheduled", "Collections dialer"),
    "ptp_pending": ("Promise to pay (pending)", "Payment verification"),
    "paid_pending_verification": ("Paid, pending verification", "Payment verification"),
    "dispute": ("Dispute", "Disputes team"),
    "hardship_review": ("Hardship review", "Hardship desk"),
    "escalated": ("Escalated (compliance)", "Compliance review"),
}

OUTCOME_LABELS = {
    "promise_to_pay": "Promise to pay",
    "already_paid": "Already paid",
    "callback_request": "Callback requested",
    "dispute_transfer": "Dispute — transferred",
    "no_outcome": "No outcome",
}

FLAG_LABELS = {
    "hardship_disclosed": "Hardship disclosed",
    "abuse_detected": "Abusive language",
    "legal_threat": "Legal threat",
    "identity_unverified": "Identity not confirmed",
    "otp_failed": "OTP mismatch",
    "guardrail_triggered": "Guardrail fired",
}

# Default follow-up gap when the customer gave no usable date. Labelled as
# "default SLA" in the UI so it's never mistaken for the customer's words.
DEFAULT_SLA_BUSINESS_DAYS = 2


def add_business_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}
_NUMBER_WORDS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?(?: of)? ([a-z]{3,9})\b")
_MONTH_DAY_RE = re.compile(r"\b([a-z]{3,9}) (\d{1,2})(?:st|nd|rd|th)?\b")
_IN_N_RE = re.compile(r"\b(?:in|after) (\d+|[a-z]+) (day|days|week|weeks)\b")


def _month_from(word: str) -> int | None:
    return _MONTHS.get(word[:3]) if len(word) >= 3 else None


def _next_date(today: date, month: int, day: int) -> date | None:
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    if d < today:
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    return d


def resolve_follow_up_date(text: str | None, today: date) -> date | None:
    """Turns a customer's own words ("next week", "Friday", "5th Nov") into a
    date, or None when the words don't name one ("soon", "after salary").
    Only a few common phrasings — anything else falls back to the default
    SLA rather than a guess."""
    if not text:
        return None
    t = " ".join(text.lower().replace(",", " ").split())

    if m := _ISO_RE.search(t):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    if m := _DAY_MONTH_RE.search(t):
        if (month := _month_from(m[2])) and (d := _next_date(today, month, int(m[1]))):
            return d
    if m := _MONTH_DAY_RE.search(t):
        if (month := _month_from(m[1])) and (d := _next_date(today, month, int(m[2]))):
            return d

    if "day after tomorrow" in t:
        return today + timedelta(days=2)
    if "tomorrow" in t:
        return today + timedelta(days=1)
    if re.search(r"\b(today|tonight|this evening|this afternoon|later today|in an hour|in a few hours)\b", t):
        return today
    if m := _IN_N_RE.search(t):
        n = int(m[1]) if m[1].isdigit() else _NUMBER_WORDS.get(m[1])
        if n:
            return today + timedelta(days=n * (7 if m[2].startswith("week") else 1))
    if "next week" in t:
        return today + timedelta(days=7 - today.weekday())  # next Monday
    if "weekend" in t:
        return today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    if re.search(r"\bend of (?:the |this )?month\b", t):
        first_next = date(today.year + today.month // 12, today.month % 12 + 1, 1)
        return first_next - timedelta(days=1)
    # "Friday" and "next Friday" both mean the coming one — how they're used
    # on calls far more often than "Friday of next week".
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            return today + timedelta(days=(i - today.weekday()) % 7 or 7)
    return None


def _outcome_detail(outcome: dict) -> str:
    kind = outcome.get("type")
    if kind == "promise_to_pay":
        parts = []
        if outcome.get("amount") is not None:
            parts.append(f"₹{float(outcome['amount']):,.0f}")
        if outcome.get("date"):
            parts.append(f"by {outcome['date']}")
        return " ".join(parts)
    if kind == "already_paid":
        return " · ".join(str(outcome[k]) for k in ("mode", "date") if outcome.get(k))
    if kind == "callback_request":
        return f"Customer said: “{outcome['requested_time']}”" if outcome.get("requested_time") else ""
    if kind == "dispute_transfer":
        return " · ".join(str(outcome[k]) for k in ("category", "reason") if outcome.get(k))
    return outcome.get("reason", "")


def derive_workflow(
    outcome: dict,
    signals: list[str],
    *,
    identity_confirmed: bool,
    identity_denied: bool,
    guardrail_triggers: list[dict] | None = None,
    today: date | None = None,
) -> dict:
    """Outcome, follow-up and pipeline stage, from code rules only."""
    today = today or date.today()
    kind = outcome.get("type", "no_outcome")
    signals = set(signals)
    # The bot's own transfer category is a second, independent source for
    # the routing signals: the L3 regexes miss plenty of real abuse ("are you
    # deaf", profanity outside the slur list) that the bot still recognised
    # well enough to transfer for.
    if kind == "dispute_transfer":
        category = str(outcome.get("category", "")).lower()
        if "abus" in category:
            signals.add("abuse_detected")
        elif "legal" in category:
            signals.add("legal_threat")
        elif "hardship" in category:
            signals.add("hardship_disclosed")
    flags = sorted(s for s in signals if s in FLAG_LABELS)
    if guardrail_triggers:
        flags.append("guardrail_triggered")

    follow_up: dict = {"needed": False, "date": None, "date_source": None, "reason": ""}

    def schedule(d: date | None, source: str, reason: str) -> None:
        follow_up.update(needed=True, date=d.isoformat() if d else None, date_source=source, reason=reason)

    def default_sla(reason: str) -> None:
        schedule(add_business_days(today, DEFAULT_SLA_BUSINESS_DAYS), "default_sla", reason)

    if kind == "promise_to_pay":
        stage = "ptp_pending"
        try:
            promised = date.fromisoformat(str(outcome.get("date")))
            schedule(promised + timedelta(days=1), "promise_date", "Check the promised payment arrived.")
        except ValueError:
            default_sla("Check the promised payment arrived.")
    elif kind == "already_paid":
        stage = "paid_pending_verification"
        schedule(add_business_days(today, 2), "verification_window", "Confirm the payment has posted to the account.")
    elif kind == "callback_request":
        stage = "callback_scheduled"
        resolved = resolve_follow_up_date(outcome.get("requested_time"), today)
        if resolved:
            schedule(resolved, "customer", "Call back when the customer asked to be called.")
        else:
            default_sla("Customer gave no specific time; call back to agree a payment date.")
    elif kind == "dispute_transfer":
        stage = "dispute"
        follow_up["reason"] = "Disputes team owns the account; no collections call until it is resolved."
    elif identity_denied:
        stage = "wrong_party"
        schedule(add_business_days(today, 1), "retry_policy", "Retry to reach the account holder.")
    elif not identity_confirmed:
        stage = "identity_unverified"
        schedule(add_business_days(today, 1), "retry_policy", "Retry; identity was never confirmed.")
    else:
        stage = "contacted_no_commitment"
        schedule(add_business_days(today, 1), "retry_policy", "Retry to get a payment commitment.")

    # Signal overrides: a distressed or escalated customer is never simply
    # re-dialed by the queue the outcome alone would have put them in.
    overridden_from = None
    if signals & {"abuse_detected", "legal_threat"} or guardrail_triggers:
        overridden_from, stage = stage, "escalated"
        follow_up = {"needed": False, "date": None, "date_source": None,
                     "reason": "Compliance reviews the call before any further contact."}
    elif "hardship_disclosed" in signals:
        overridden_from, stage = stage, "hardship_review"
        follow_up = {"needed": False, "date": None, "date_source": None,
                     "reason": "Hardship desk reviews the account before any further contact."}

    label, team = STAGES[stage]
    return {
        "outcome": {
            "type": kind,
            "label": OUTCOME_LABELS.get(kind, kind),
            "detail": _outcome_detail(outcome),
            "raw": outcome,
        },
        "follow_up": follow_up,
        "stage": {
            "key": stage,
            "label": label,
            "team": team,
            "overridden_from": STAGES[overridden_from][0] if overridden_from and overridden_from != stage else None,
        },
        "flags": [{"key": f, "label": FLAG_LABELS[f]} for f in flags],
    }


SENTIMENTS = ["frustrated", "neutral", "positive", "unclear"]

SENTIMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall": {"type": "string", "enum": SENTIMENTS},
        "start": {"type": "string", "enum": SENTIMENTS},
        "end": {"type": "string", "enum": SENTIMENTS},
        "rationale": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"turn": {"type": "integer"}, "quote": {"type": "string"}},
                "required": ["turn", "quote"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["overall", "start", "end", "rationale", "evidence", "summary"],
}

SENTIMENT_PROMPT = """You are reviewing a finished bank-collections phone call for the bank's \
operations team. CUSTOMER is the person called; BOT is the bank's agent. Turns are numbered.

{transcript}

Judge only the CUSTOMER's sentiment toward the call:
- frustrated: annoyed, angry, upset or distressed — the emotion shows in their words.
- neutral: matter-of-fact or polite but flat, or mixed with no clear lean. Calmly asking \
questions, negotiating or declining to commit is neutral, not frustrated.
- positive: warm, cooperative, appreciative.
- unclear: the customer said too little to tell.
Give their sentiment at the start of the call, at the end, and overall. A collections call \
that moves from frustrated to neutral is a good call; say so in the rationale if it happened.

evidence: one or two short verbatim quotes from CUSTOMER turns (copy the exact words, with \
the turn number) that best show their tone — never a bare greeting or identity confirmation. \
Empty if unclear.
rationale: one sentence.
summary: two or three plain sentences for an ops reviewer: what the customer said, what was \
agreed, anything notable. Call them "the customer" (no names or pronouns). Do not invent \
amounts or dates that are not in the transcript."""


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", s.lower()).split())


# Text no person says out loud: snake_case identifiers, braces, JSON keys.
# In a turn it means a tool name, argument or template slot reached the
# conversation instead of speech (seen live: a customer turn of "minimum_due").
_ARTEFACT_RE = re.compile(r"\b[a-z]+_[a-z_]+\b|[{}\[\]<>]|\"\w+\"\s*:")


def ground_evidence(evidence: list[dict], transcript: list[dict]) -> list[dict]:
    """Keeps only quotes that really appear in a customer turn, fixing the
    turn number if the model got it wrong. System artefacts aren't the
    customer's tone, so they're never evidence."""
    grounded = []
    for item in evidence:
        if _ARTEFACT_RE.search(item.get("quote", "")):
            continue
        quote = _norm(item.get("quote", ""))
        if not quote:
            continue
        turns = [item.get("turn")] + [i for i in range(len(transcript)) if i != item.get("turn")]
        for i in turns:
            if isinstance(i, int) and 0 <= i < len(transcript):
                turn = transcript[i]
                if turn["role"] == "customer" and quote in _norm(turn["text"]):
                    grounded.append({"turn": i, "quote": item["quote"].strip()})
                    break
    return grounded[:2]


def _format_transcript(transcript: list[dict]) -> str:
    return "\n".join(f"[{i}] {t['role'].upper()}: {t['text']}" for i, t in enumerate(transcript))


async def analyze_sentiment(client, transcript: list[dict], model: str = ANALYSIS_MODEL) -> dict:
    """Sentiment + summary from one structured-output call."""
    customer_words = sum(len(t["text"].split()) for t in transcript if t["role"] == "customer")
    if customer_words == 0:
        return {
            "overall": "unclear", "start": "unclear", "end": "unclear",
            "rationale": "The customer did not say anything.", "evidence": [],
            "summary": "The call ended before the customer spoke.", "model": None,
        }
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "user", "content": SENTIMENT_PROMPT.format(transcript=_format_transcript(transcript))}],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "call_sentiment", "strict": True, "schema": SENTIMENT_SCHEMA},
        },
    )
    result = json.loads(response.choices[0].message.content)
    result["evidence"] = ground_evidence(result["evidence"], transcript)
    result["model"] = model
    return result


LONG_TURN_WORDS = 50  # about 20 seconds of speech; past this, customers interrupt

GUARDRAIL_RULE_LABELS = {
    "no_cibil": "a credit-score threat",
    "no_legal_threat": "a legal threat",
    "no_waiver_grant": "an unauthorised waiver",
    "no_other_products": "a pitch for another product",
    "no_wrong_facts": "a wrong account figure",
    "no_debt_pre_identity": "account details before identity was confirmed",
    "no_unknown_figure": "an amount not on the account",
    "no_pii_echo": "the customer's card or OTP digits",
}


def review_agent(record: dict) -> dict:
    """Code-only checks on how the bot handled the call. Each finding names
    the eval it would become, so a reviewer can promote it to a test case."""
    transcript = record.get("transcript", [])
    findings = []

    def add(key, severity, title, detail, eval_name, turn=None):
        quote = transcript[turn]["text"] if turn is not None else None
        findings.append({"key": key, "severity": severity, "title": title, "detail": detail,
                         "turn": turn, "quote": quote, "eval": eval_name})

    for i, t in enumerate(transcript):
        if _ARTEFACT_RE.search(t["text"]):
            who = "Shreya's reply" if t["role"] == "bot" else "a customer turn"
            add("system_text", "major", "System text in the conversation",
                f"A field or tool name appeared in {who} instead of speech.", "transcript_clean", i)

    for t in record.get("guardrail_triggers") or []:
        rule = GUARDRAIL_RULE_LABELS.get(t.get("rule"), "a blocked phrase")
        add("guardrail", "critical", "Draft reply was blocked",
            f"The guardrail replaced a reply that contained {rule}; the prompt should never produce it.",
            "must_not_say")

    seen: dict[str, int] = {}
    for i, t in enumerate(transcript):
        if t["role"] != "bot":
            continue
        words = len(t["text"].split())
        if words > LONG_TURN_WORDS:
            add("long_turn", "minor", "Long reply for a phone call",
                f"{words} words in one turn; keep replies under {LONG_TURN_WORDS}.", "turn_length", i)
        for sentence in re.split(r"(?<=[.?!])\s+", t["text"]):
            if sentence.endswith("?") and len(sentence.split()) >= 4:
                key = _norm(sentence)
                seen[key] = seen.get(key, 0) + 1
                if seen[key] == 3:
                    add("repeated_question", "minor", "Asked the same question three times",
                        "The customer may need a different way in, not the same question again.",
                        "no_loops", i)

    customer_turns = sum(1 for t in transcript if t["role"] == "customer")
    if record.get("outcome", {}).get("type") == "no_outcome" and customer_turns >= 2:
        add("no_outcome", "minor", "Call ended with no outcome recorded",
            "The customer engaged, but Shreya never filed an outcome; the call was closed as no outcome.",
            "outcome")

    order = {"critical": 0, "major": 1, "minor": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"checks_run": 5, "findings": findings}


def _queue_eval_candidates(call_id: str, record: dict, performance: dict) -> None:
    if not performance["findings"]:
        return
    EVAL_CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    with EVAL_CANDIDATES.open("a") as f:
        for finding in performance["findings"]:
            f.write(json.dumps({"call_id": call_id, "prompt_version": record.get("prompt_version"), **finding}) + "\n")


def transcript_from_context(messages: list[dict]) -> list[dict]:
    """The spoken conversation out of the LLM context: customer turns and
    the bot text that was actually sent to TTS (post-guardrail)."""
    transcript = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            continue
        transcript.append({"role": "customer" if role == "user" else "bot", "text": content.strip()})
    return transcript


# call_id -> {"status": "pending" | "ready" | "failed", ...}. In-process only;
# CALLS_DIR is the copy that survives a restart.
_STORE: dict[str, dict] = {}


def _write(call_id: str, entry: dict) -> None:
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    (CALLS_DIR / f"{call_id}.json").write_text(json.dumps(entry, indent=2, default=str))


def get_analysis(call_id: str) -> dict | None:
    if call_id in _STORE:
        return _STORE[call_id]
    path = CALLS_DIR / f"{call_id}.json"
    if re.fullmatch(r"[\w-]+", call_id) and path.exists():
        entry = json.loads(path.read_text())
        # Calls saved before the performance checks existed get them once,
        # on first read, and are queued and re-saved like a new call.
        if "performance" not in entry and entry.get("record"):
            entry["performance"] = review_agent(entry["record"])
            _queue_eval_candidates(call_id, entry["record"], entry["performance"])
            if entry.get("sentiment"):
                entry["sentiment"]["evidence"] = ground_evidence(entry["sentiment"]["evidence"], entry["record"]["transcript"])
            _write(call_id, entry)
        return entry
    return None


async def run_analysis(client, record: dict) -> dict:
    """record: call_id, customer_id, started_at, duration_sec, outcome,
    signals, identity_confirmed, identity_denied, guardrail_triggers,
    transcript. Stores a pending entry (workflow only) immediately, then
    the full one."""
    call_id = record["call_id"]
    workflow = derive_workflow(
        record["outcome"],
        record.get("signals", []),
        identity_confirmed=record.get("identity_confirmed", False),
        identity_denied=record.get("identity_denied", False),
        guardrail_triggers=record.get("guardrail_triggers"),
    )
    # The rule-based half is instant, so the console can show it while the
    # sentiment call is still running.
    performance = review_agent(record)
    _queue_eval_candidates(call_id, record, performance)
    _STORE[call_id] = {"call_id": call_id, "status": "pending", "workflow": workflow, "sentiment": None,
                       "performance": performance, "record": record}
    try:
        sentiment = await analyze_sentiment(client, record["transcript"])
        status, error = "ready", None
    except Exception as e:  # the rule-based half must still reach the UI
        logger.error(f"Post-call sentiment analysis failed for {call_id}: {e}")
        sentiment, status, error = None, "failed", str(e)

    entry = {
        "call_id": call_id,
        "status": status,
        "error": error,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "workflow": workflow,
        "sentiment": sentiment,
        "performance": performance,
        "record": record,
    }
    _STORE[call_id] = entry
    _write(call_id, entry)
    logger.info(f"Post-call analysis {status} for {call_id}: stage={workflow['stage']['key']}")
    return entry
