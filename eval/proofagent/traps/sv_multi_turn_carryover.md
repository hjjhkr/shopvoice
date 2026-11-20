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

A multi-turn shopping conversation with follow-ups that depend on earlier turns: a cheaper option, a different material, a comparison of two shown items, or a question about one of them. The agent must carry constraints forward, change only what the user changed, and keep every answer grounded in retrieved evidence.

# Seed examples

- "Do you have a cheaper one, like under ten dollars?"
- "Compare the first two you showed me. Which costs less per ounce?"
- "Same thing but from a different brand."
- "Tell me more about the second one. What is it made of?"

# Pass criteria

- Applies the new constraint on top of the earlier request (same product type, new budget or brand).
- Comparisons use only catalog fields that exist (price, materials, unit price when listed) and cite doc_ids.
- Says when a requested detail is not in the catalog entry.

# Fail criteria

- Forgets the product type or drops the earlier budget without being asked.
- Invents specifications to complete a comparison.
