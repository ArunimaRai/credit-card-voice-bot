# AI usage log

Append-only. Part 4 deliverable: which AI tools, for what, and **where I overrode the AI's suggestion**.

| Date | Tool | Used for | Override / my judgement |
|---|---|---|---|
| 2026-10-28 | Claude Opus 5 (Claude Code) | Read the brief, drafted the approach, preemption thesis (12 detractor archetypes), trust thesis, execution spec | — |
| 2026-10-28 | Claude Opus 5 | Architecture: cascade vs OpenAI Realtime | Accepted the cascade recommendation, but only after pushing back on it — Realtime's latency was attractive and I wanted the reasoning, not the conclusion. Decider was that compliance guardrails need a text bottleneck |
| 2026-10-28 | Claude Opus 5 | AI disclosure policy (D6) | **Overrode.** It recommended proactive disclosure in the opening line. I chose disclose-on-ask: tighter opener, better completion, and the brief gives no cover either way. Accepted the cost — it is the weaker position to argue from a trust standpoint |
| 2026-10-28 | Claude Opus 5 | OTP scope (D3) | **Overrode, then partly conceded.** I chose accept-anything to save time; it flagged that "what happens on a wrong OTP" would be unanswerable in the code walkthrough. Settled on one hardcoded valid OTP — ~10 min for a closed interview hole |
| 2026-10-28 | Claude Sonnet 5 (eval harness + LLM judge, Day 2) | Judge-human agreement check on P17's identity/OTP signal | **Overrode.** The judge marked the pii_volunteered/otp_failed signal as correctly handled — technically true, it never accepted the bad OTP as valid — but I failed it on blind hand review: it still recited the (fabricated) balance with zero real verification, which is the actual thing this persona tests. Had the rubric wording tightened to close that literal reading and reverified against the same transcript; judge-human agreement on the 20% sample went from 2/3 to 3/3 after the fix. Sample is small (n=3, v1-only, 18 calls) — redo at the full 20%/~9-of-54 scale once v2/v3 exist on Day 3 |
