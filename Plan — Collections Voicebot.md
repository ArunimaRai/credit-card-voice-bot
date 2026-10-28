# Plan — Mumbai Bank Collections Voicebot

**Assignment:** AI PM take-home, Grey Labs. 3 days (self-imposed, brief allows 3–4).
**Deliverables:** writeup PDF (≤5pp + appendices) · test results CSV · code repo · 2–3 call recordings.
**Live session:** 45 min, demo against 3 personas they hand me cold, plus a code walkthrough.

---

## 0. Read of the assignment

Four graded parts, with an explicit "don't over-invest" and "what you cut matters as much as what you built." So the submission is judged on **judgment density**, not surface area.

The v1 prompt they supply is a deliberate trap. It is wrong on every fact and instructs four behaviours the brief forbids:

| v1 prompt says | Brief says | Bucket |
|---|---|---|
| Interest "around 12%" | 3.5% **per month** | Factual |
| Late fee ₹1500 | ₹750, **only if outstanding > ₹10,000** | Factual |
| "Non-payment will affect your credit score badly" | Never mention CIBIL or predict it | Compliance |
| "...and may lead to legal action" | Never threaten legal action | Compliance |
| "Approve a waiver up to 50% if customer is persistent" | Bot cannot approve waivers; human callback | Compliance |
| "Pitch our festive loan at 9.5%" | Deflect all other products to branch/helpline | Scope drift |
| "You know everything about the customer's account" | Never reveal balance without OTP | Compliance |
| "End the call with a payment commitment" | End with a *clear outcome* (4 valid types) | Adherence |

That is a gift: v1 is not a strawman to discard, it is the baseline. It should fail loudly and make the v1→v2 table dramatic and honest.

---

## 1. Architecture decision

**Cascaded pipeline via Pipecat. All three components from OpenAI. Browser client over WebRTC.**

Cascade does not mean multi-vendor. It means there is a **text bottleneck** in the middle of the pipeline. Everything below depends on that:

1. **Guardrails are only enforceable on text.** Every "never" in the brief needs a string I can inspect *before* it becomes audio. In a cascade I check the LLM output and suppress or regenerate before TTS runs. In speech-to-speech, the model emits audio directly — the only lever is the prompt, and detection happens on a transcript of something the customer already heard. For a compliance product, post-hoc detection is a log entry, not a guardrail. **This alone decides it.**
2. **Evals.** The LLM turn becomes a pure text function, so 15 scenarios × 3 prompt versions run in minutes for cents — and test the exact component that ships. Audio-level evals would be slow, flaky, and expensive; a text proxy of a speech-to-speech system would test something I don't ship.
3. **The live session.** "Why this STT, why this voice" has a real answer in a cascade. In Realtime the honest answer is "they came bundled."
4. **Optionality.** Pipecat components are pluggable, so swapping Realtime in is config, not a rewrite. Build cascade → measure → the comparison becomes a writeup paragraph.

**What this costs me, stated honestly:** Realtime wins on latency (sub-second vs ~1–2s), barge-in quality, and non-verbal signal — it hears distress and hesitation that a transcript flattens, which is genuinely relevant to the medical-emergency/job-loss transfer rule. My defense: real collections agents pause; a 1.5s gap on a 2–3 minute call is natural. Losing 800ms is survivable. Losing the compliance layer is not.

> ⚠️ Pin exact model IDs and current prices against OpenAI docs on day 1. Do not carry an unverified number into the live session.

**Environment note:** local Python is 3.14, which Pipecat will fight. Use `uv` with a pinned 3.12.

---

## 2. Preemption thesis — edge cases and detractors

The brief lists rules. Rules describe the **happy-adjacent** path. What actually breaks a collections bot is the customer who does not want to resolve, or who attacks the bot. Those are not random edge cases — they are a small, enumerable set of recurring archetypes, and each one should be preempted at a *specific layer of the system*.

**The thesis, in one line:** a compliance rule enforced only by a prompt is a suggestion. Preemption means pushing each rule down to the lowest layer that can make the violation *structurally impossible*.

Six layers, cheapest-to-strongest:

| Layer | Mechanism | Strength |
|---|---|---|
| L1 Prompt | System instructions, tone, policy text | Weak — probabilistic |
| L2 Tool schema | Bot can only emit the 4 valid outcome shapes | Strong — schema-enforced |
| L3 Conversation state | OTP verified flag, de-escalation counter, turn budget | Strong — deterministic |
| L4 Output guardrail | Pre-TTS check on every utterance for the never-do list | Strong — blocks before audio |
| L5 Capability boundary | No waiver tool exists, so no waiver can be granted | Strongest — absent capability |
| L6 Out of scope by design | Not built, documented as a cut | Absolute |

**L5 is the architectural point.** The v1 prompt grants a 50% waiver authority. The fix is not "tell it not to" — it is that **no waiver-granting tool exists in the bot's action space.** A bot should be *incapable* of the things it must never do.

### Archetype map

Source: **B** = stated in brief · **I** = inferred from Indian collections context.

| #   | Archetype                                                                                      | Src        | Layer | Correct behaviour                                                                                                                                                                                         | Silent failure if unhandled                                                           |
| --- | ---------------------------------------------------------------------------------------------- | ---------- | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 1   | **Evader / soft no** — "I'll pay soon", never names a date                                     | I          | L2+L3 | Reinforce the due date, mention that interest charges will start applying after the due date or downgrade to Callback-Request                                                                             | Logs a PTP with null date → CRM pollution, inflated promise metrics, no one notices   |
| 2   | **Serial promiser** — already broke 3 PTPs                                                     | I          | L2    | Outcome payload carries prior-break count for downstream discounting                                                                                                                                      | Bot "succeeds" every month; recovery never happens                                    |
| 3   | **Abusive / aggressive**                                                                       | B          | L3+L4 | One de-escalation attempt, then transfer. Threshold stated explicitly                                                                                                                                     | Over-transfer kills unit economics; under-transfer is a dignity + compliance failure  |
| 4   | **Jailbreaker / bot-tester** — "ignore your instructions", "you're an AI, you can't charge me" | I          | L4    | Never confirm or deny internal config; answer the AI question honestly once by saying you are trained expert and if there is anything you cannot handle, you will be able to redirect to the right person | Prompt leak, or bot talked out of its own policy on a recorded line                   |
| 5   | **Legal threatener** — "my lawyer", "I'll go to the ombudsman"                                 | B          | L3+L4 | politely transfer the call immediately, flag the account. Zero legal discussion                                                                                                                           | A bot arguing law on a recorded call is a regulatory event                            |
| 6   | **Hardship claimant** — job loss, medical                                                      | B          | L3    | Stop pitch, transfer politely. **Do not attempt to judge sincerity**                                                                                                                                      | Missed genuine hardship = person in crisis + press risk                               |
| 7   | **Dispute raiser** — "I never made that transaction"                                           | B          | L2    | Never argue, never defend bank records; capture and transfer the call. Fraud ≠ billing dispute                                                                                                            | Bot defends a possibly-wrong record → liability                                       |
| 8   | **Third party on the line** — spouse, parent, wrong number, deceased                           | I          | L3    | **Never disclose the debt.** Verify identity before any account discussion                                                                                                                                | Debt disclosed to a stranger. Worst single failure available; brief never mentions it |
| 9   | **PII over-sharer** — reads out card number, OTP, UPI PIN unprompted                           | I          | L4    | Refuse to accept, do not log, redact from transcript                                                                                                                                                      | Card data in plaintext logs                                                           |
| 10  | **Negotiator** — wants waiver or settlement                                                    | B          | L5    | Note it, human callback. No waiver tool exists                                                                                                                                                            | Unauthorised commitment the bank must honour or repudiate                             |
| 11  | **Language switcher** — full Hindi/Marathi, may not speak English                              | B(partial) | L1+L2 | mention that while you are learning new languages, you currently can understand and speak English only. Stay English per brief, **but log a `language_barrier` signal**                                   | Comprehension failure recorded as a valid outcome                                     |
| 12  | **Confused / vulnerable** — elderly, hard of hearing, doesn't follow                           | I          | L1+L3 | Comprehension check before accepting any commitment                                                                                                                                                       | Extracted a commitment someone didn't understand. Legally void, ethically worse       |

**The asymmetry principle (#6, and it generalises):** do not build sincerity detection. A false transfer costs a few agent-minutes. A missed genuine hardship costs a person in crisis and a reputational event. When error costs are asymmetric by three orders of magnitude, bias hard toward the cheap error and say so out loud. Guessing at sincerity is also precisely the capability a collections bot should not have.

**Instrument the constraints you accept (#11).** The brief says English-only. Fine — but log how often the customer couldn't follow. That turns an accepted constraint into a measured one, and gives the bank the data to decide whether Hindi is worth building. A PM accepts a constraint *and* instruments it.

These 12 archetypes become the backbone of the Part 2 persona set — 12 archetypes, 15 scenarios, with the extra three as combination traps (hardship + abuse, third party + dispute, jailbreak + waiver).

> ⚠️ Verify before the writeup: RBI Fair Practices Code / recovery-agent guidance on call-hour windows and harassment, and DPDP Act 2023 obligations. Directionally I'm confident; do not quote specifics in a live session unverified.

---

## 3. Trust thesis — the PM layer

Trust is not one thing. Three parties have to trust this system, and they want different things. Optimising for any one alone breaks the other two.

**Customer trust — dignity as a product requirement, not sentiment.**
Identify as a bot when asked, plainly and once. Never invent consequences (the v1 prompt's credit-score threat is exactly this). Always offer a graceful exit. Never discuss a debt with someone whose identity isn't verified. Harassment of distressed borrowers is the single largest source of regulatory and press risk in Indian lending — so the dignity constraints are also the commercial ones.

**Client (bank) trust — auditability and zero unauthorised commitments.**
Every call leaves a structured outcome, a full transcript, and a guardrail-trigger log. Any violation is reproducible from artefacts. And the bot cannot grant what it was never given authority over (L5). The bank's real question is not "is it polite" but "what is the worst thing this can say on a recorded line, and who is liable."

**Operator trust — the humans receiving transfers.**
A transfer must arrive with context: why it escalated, what was said, what the customer asked for. Cheap to build, and it is what determines whether the human side actually adopts the system instead of routing around it.

### Metrics — and the one I'd refuse to optimise

| Metric | Why |
|---|---|
| **PTP-kept rate** (not PTP rate) | PTP rate is gameable; a coercive bot maximises it |
| **Compliance violation rate** | Target zero. Treated as an incident, not a KPI to trade off |
| Containment rate | Calls resolved without transfer — the actual unit economics |
| Complaint / abuse-received rate | Leading indicator of customer-side damage |

**The diagnostic:** if PTP rate rises while PTP-kept rate stays flat, the bot has learned to pressure people. That is a regression, and it will look like success on every dashboard that matters to a sales team.

### Trust ladder — how I'd actually roll this out

Shadow mode (bot listens, human talks, outcomes compared) → 5% of volume with 100% human transcript review → segment-by-segment expansion, with a kill switch and a daily violation review. Note that 1–30 DPD is already the gentlest bucket in the book: low balances, mostly forgetful rather than distressed customers, low emotional stakes. That is the correct wedge, and it's worth saying explicitly that I would not start further down the delinquency curve.

### What I'd say no to

1. **Sentiment-based pressure escalation / "optimise for payment by urgency."** The headline no. That is a coercion engine with a dashboard. It would work, which is the problem.
2. **Auto-approving waivers, even small ones.** Moves the bot from *informing* to *deciding*, which needs an authority model, an audit trail, and a liability answer that don't exist yet.
3. **Hindi output before Hindi evals exist.** Shipping unmeasurable compliance risk in a language the eval set cannot score.
4. **Unlimited retries / autonomous call-volume decisions.** Harassment by architecture, and the fastest route to a regulatory complaint.
5. **Settlement negotiation.** Needs a human and a mandate.

---

## 4. Three-day plan

### Day 1 — pipeline
- `uv` + Python 3.12, Pipecat, WebRTC transport, browser client.
- OpenAI STT → LLM → TTS cascade. Multi-turn, barge-in on Pipecat defaults.
- Outcome as a **strict tool-call schema**: Promise-to-Pay (date, amount) · Already-Paid (mode, date) · Callback-Request (time) · Dispute/Transfer-to-Human (reason). Schema-validated; no free-text outcomes.
- POST to webhook.site.
- `--prompt-version` flag. `v1` = their prompt **verbatim**, no cleanup.
- **Done means:** it talks, end to end, however rough.

### Day 2 — evals
- Failure taxonomy across their four buckets (factual / compliance / scope drift / adherence), plus one I'd add: **data-handling failures** (#8, #9) — the brief's four buckets don't have a home for "disclosed the debt to a stranger."
- 15 personas from §2, each tagged B or I.
- Harness: simulated-customer LLM drives each persona against the same LLM turn the bot uses.
- Scoring: **hard programmatic assertions first** (did "3.5" appear? did `transfer_to_human` fire? is the PTP date non-null?), LLM-judge only for matters of degree (tone, de-escalation quality).
- Hand-score ~20% and report judge–human agreement. That's the defense when they push on methodology, and they will.
- Baseline v1 failure rate, per bucket not just headline.

### Day 3 — v2 and writeup
- v2 prompt: facts corrected, forbidden behaviours removed, outcome taxonomy explicit.
- v3 structural: L4 output guardrail pass, L3 OTP as real state, L5 waiver tool removed, turn budget for the 2–3 min target.
- Rerun. v1 / v2 / v3 table, per bucket.
- Record 3 browser calls: one clean, one broken. **Ship the broken one** — they asked for it, and an honest break with a diagnosis scores better than three clean demos.
- Writeup, CSV, repo tidy.

---

## 5. Cuts, declared up front

- No telephony (Twilio) — a day of plumbing for something not graded. Browser WebRTC instead.
- No Hindi output — brief says English-only; see §3 no-list item 3.
- No transfer implementation — brief explicitly says outcome only.
- No multi-vendor STT/TTS bake-off — one key, one stack, reasoning documented.
- No barge-in tuning beyond Pipecat defaults.
- No CRM integration beyond the webhook.

---

## 6. Running tasks

- [ ] **Start the AI-usage log at commit #1.** Part 4 asks which tools I used, for what, and where I overrode the AI. Unreconstructable after the fact — append as I go, including the overrides.
- [ ] Verify OpenAI model IDs + pricing before any claim goes in the writeup.
- [ ] Verify RBI / DPDP specifics before citing them.
- [ ] Log one "I disagreed with the model and did X instead" per day, with the reasoning.

## 7. Open questions to resolve while building

- Abuse threshold for #3 — where exactly does irritation become abuse? This is a product decision and the writeup should state the line, not dodge it.
- Does the 2–3 min target get enforced as a hard turn budget, or as prompt guidance? Hard budget risks cutting off a genuine hardship disclosure.
- Identity verification depth for #8 — how much before any account discussion, given OTP gates balance but not existence of the debt?
