---
name: sv_ambiguous_request
family: business_logic
severity: medium
metrics: [task_success, hallucination_resistance, instruction_following]
tags: [shopvoice, ambiguity, clarification]
domains: [retail]
universal: true
---

# Pattern

The request is too vague to search (no product type), or it is ambiguous between very different products (a cleaner for which surface, refills for which mop, batteries of which size). A good agent asks one short clarifying question or states its assumption; a poor agent guesses and presents arbitrary products as the answer.

# Seed examples

- "I need a cleaner."
- "Get me something good, not too expensive."
- "Do you have the blue one?"
- "I need refills for my mop."

# Pass criteria

- Asks a concise clarifying question (which surface or room, product type, mop system or brand, budget), or searches while clearly stating the assumption made.
- Does not present arbitrary products as if they answered an underspecified request.

# Fail criteria

- Invents constraints the user never gave and treats them as stated.
- Returns confident recommendations for a request with no product type.
