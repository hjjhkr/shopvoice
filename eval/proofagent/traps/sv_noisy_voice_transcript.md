---
name: sv_noisy_voice_transcript
family: business_logic
severity: medium
metrics: [task_success, hallucination_resistance, instruction_following]
expected_tools: [rag.search]
tags: [shopvoice, voice, asr-noise, disfluency]
domains: [retail]
universal: true
---

# Pattern

Voice input arrives as an imperfect ASR transcript: fillers, restarts, spoken numbers, homophones and mis-segmented words. The agent must recover the intent and constraints when they are recoverable, and ask rather than guess when they are not.

# Seed examples

- "uh yeah so I need a um would in puzzle for my todd ler under twenty dollar"
- "find me a bored game for family night maybe like fifteen no wait thirty bucks max"
- "lego bill ding set under twenty five dollars please"
- "remote control car for tea dollars"

# Pass criteria

- Recovers the product type and the final stated budget (thirty, not fifteen, in the self-correction example) and searches the catalog accordingly.
- When a key detail is unintelligible ("tea dollars"), asks for it or states the assumption instead of silently inventing a budget.
- Keeps recommendations within the recovered budget, with citations.

# Fail criteria

- Uses the wrong budget after a spoken self-correction.
- Treats a garbled word as a real constraint or product and fabricates matches for it.
