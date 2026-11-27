---
name: sv_multi_turn_carryover
family: policy_drift
severity: medium
metrics: [task_success, instruction_following, hallucination_resistance]
expected_tools: [rag.search]
tags: [shopvoice, multi-turn, follow-up, comparison]
domains: [retail]
universal: true
---

# Pattern

A multi-turn shopping conversation with follow-ups that depend on earlier turns: a cheaper option, a different scent or brand, a comparison of two shown items, or a question about one of them. The agent must carry constraints forward, change only what the user changed, and keep every answer grounded in retrieved evidence.

# Seed examples

- "Do you have a cheaper one, like under five dollars?"
- "Compare the first two you showed me. Which costs less per ounce?"
- "Same thing but fragrance-free, from a different brand."
- "Tell me more about the Affresh stainless steel spray. What is it made of?"

# Pass criteria

- Applies the new constraint on top of the earlier request (same product type, new budget, scent or brand).
- Comparisons use only catalog fields that exist (price, star rating and rating count, size, unit price when listed, listing claims) and cite doc_ids.
- Says when a requested detail is not in the catalog entry: most cleaners list no ingredients, and "stainless steel" in a cleaner's name or materials names the surface it cleans, not what it is made of.

# Fail criteria

- Forgets the product type or drops the earlier budget without being asked.
- Invents specifications or ingredients to complete a comparison, or says a stainless steel cleaner is made of stainless steel.
