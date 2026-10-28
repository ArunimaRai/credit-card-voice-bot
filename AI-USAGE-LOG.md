# AI usage log

Append-only. Part 4 deliverable: which AI tools, for what, and **where I overrode the AI's suggestion**.

| Date | Tool | Used for | Override / my judgement |
|---|---|---|---|
| 2026-10-28 | Claude Opus 5 (Claude Code) | Read the brief, drafted the approach, preemption thesis (12 detractor archetypes), trust thesis, execution spec | — |
| 2026-10-28 | Claude Opus 5 | Architecture: cascade vs OpenAI Realtime | Accepted the cascade recommendation, but only after pushing back on it — Realtime's latency was attractive and I wanted the reasoning, not the conclusion. Decider was that compliance guardrails need a text bottleneck |
| 2026-10-28 | Claude Opus 5 | AI disclosure policy (D6) | **Overrode.** It recommended proactive disclosure in the opening line. I chose disclose-on-ask: tighter opener, better completion, and the brief gives no cover either way. Accepted the cost — it is the weaker position to argue from a trust standpoint |
| 2026-10-28 | Claude Opus 5 | OTP scope (D3) | **Overrode, then partly conceded.** I chose accept-anything to save time; it flagged that "what happens on a wrong OTP" would be unanswerable in the code walkthrough. Settled on one hardcoded valid OTP — ~10 min for a closed interview hole |
