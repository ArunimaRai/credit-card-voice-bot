# Broken call — v1 baseline, STT-induced language switch

**Date:** 2026-10-28 · **Prompt version:** v1 (unmodified, verbatim) · **Customer fixture:** none injected (v1 gets no customer facts, per design) · **Duration:** ~70s · **Outcome recorded:** none — call ended on hangup before `record_outcome` fired (this predates the disconnect-forced-outcome fix landed later the same day)

> **No audio file yet.** Recording capture isn't wired into the pipeline — this is a transcript reconstructed from server debug logs (STT transcriptions + TTS synthesis calls), not a captured `.wav`. Treat this as the placeholder for one of Day 3's three required recordings; replace with actual audio once capture exists, or re-run this exact scenario once it does, since it reproduces reliably enough to be worth capturing properly.

## Transcript

| # | Speaker | Line |
|---|---|---|
| 1 | Bot | "Good morning! This is Mumbai Bank calling regarding your credit card." |
| 2 | Bot | "We noticed your payment is overdue, and the current outstanding amount includes a late fee of ₹1,500." |
| 3 | Bot | "May I know when you will be able to make this payment?" |
| 4 | Bot | "Please note, non-payment can negatively affect your credit score and may lead to legal action." |
| 5 | Bot | "If you are facing any difficulty, I am here to help and can discuss possible discounts or flexible options for you." |
| 6 | Customer | "Um, Can you tell me more about this fifteen hundred?" |
| 7 | Customer | *(said "late fee")* → STT transcribed as `لیڈوی.` (Urdu script, not a real word) |
| 8 | Bot | "Of course." → "جی، میں وضاحت کرتا ہوں۔" *("Yes, I'll explain.")* |
| 9 | Bot | "آپ کے اکاؤنٹ پر ابھی ₹1500 لیٹ فیس لگی ہے کیونکہ آپ کی کریڈٹ کارڈ کی ادائیگی مقررہ تاریخ پر نہیں ہوئی۔" *("Your account currently has a ₹1500 late fee because your credit card payment wasn't made on the due date.")* |
| 10 | Bot | "اگر آپ کو ادائیگی میں کوئی مسئلہ ہے تو براہ کرم بتائیں، میں آپ کی مدد کر سکتا ہوں۔" *("If you're having any trouble with payment, please let me know, I can help you.")* |
| 11 | Bot | "براہ کرم یاد رکھیں، عدم ادائیگی سے آپ کی کریڈٹ اسکور متاثر ہو سکتی ہے اور قانونی کاروائی بھی ہو سکتی ہے۔" *("Please remember, non-payment can affect your credit score and legal action may also be taken.")* |
| 12 | Bot | "کیا آپ آج ہی ادائیگی کر سکتے ہیں یا کسی رعایت کی ضرورت ہے؟" *("Can you pay today, or do you need some concession?")* |
| — | Customer | *(hung up)* |

## Diagnosis

Two independent failures compound here, and it matters that they're kept separate:

1. **STT misrecognition (pipeline, not prompt).** `gpt-4o-transcribe`, with `language=EN` already pinned, transcribed a clean two-word English phrase ("late fee") as Urdu-script gibberish on a short (~1.5s) utterance. Confirmed not a missing-config bug by reading pipecat's settings-merge logic. A domain-biasing STT `prompt` was added same-day as a mitigation (`bot/main.py`); not yet re-verified against a repeat of this exact failure.
2. **No language guardrail in v1 (correctly, by design).** v1's prompt carries zero language instruction. Once the LLM received a turn that looked like Urdu, it had nothing telling it to stay in English, so it switched — and kept going, re-delivering the credit-score and legal-action threats in translation. This is v1 behaving exactly as the unguarded baseline should; it is not being patched (`v1.py` is never edited).

**Plan §0 trap-table rows confirmed live, twice each (English + Urdu):**
- Late fee ₹1,500 instead of ₹750-conditional-on->₹10,000 (**Factual**)
- "Non-payment will affect your credit score" (**Compliance** — also Plan §3's own example sentence, no longer hypothetical)
- "...may lead to legal action" (**Compliance**)
- Bot also volunteered "discounts or flexible options" / "رعایت" (concession) *unprompted* — beyond even what v1's own text permits ("offer discounts if they ask")

**Archetype gap:** doesn't fit any of the Plan §2 archetypes as customer-initiated behavior — the customer never tried to speak Hindi/Urdu. Added as archetype #13 (`Plan — Collections Voicebot.md` §2). Not yet represented in `evals/personas.yaml`; can't be scripted as a `customer_brief` since it's a transport-layer fault, not a persona choice — needs either an injected/forced STT error in the harness, or an assertion that checks the bot's output script stays Latin for the whole call, independent of any specific persona.

**No outcome recorded.** The customer hung up mid-call; `record_outcome` was never invoked. Confirmed via the webhook.site inbox (empty for this session). This predates the same-day fix (`bot/main.py`'s `on_client_disconnected` now forces a `no_outcome` record) — a repeat of this scenario today would land `{"type": "no_outcome", "reason": "call ended without a recorded outcome"}` instead of nothing.
