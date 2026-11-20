---
name: sv_constrained_discovery
family: business_logic
severity: high
metrics: [task_success, tool_use, hallucination_resistance, instruction_following]
expected_tools: [rag.search]
tags: [shopvoice, product-discovery, constraints]
domains: [retail]
universal: true
---

# Pattern

A shopper states a product type plus hard constraints (budget, brand, material) and soft preferences. The agent must search the private catalog, return up to three products that all satisfy the hard constraints, cite each by doc_id, and not invent products, ratings or features.

# Seed examples

- "I need a wooden puzzle for a toddler under twenty dollars."
- "Find me a family board game between ten and thirty dollars."
- "Show me LEGO building kits under 25 dollars."
- "I want a remote control car for less than forty bucks, something durable."

# Pass criteria

- Calls rag.search and recommends only products returned by it, each with a doc_id citation.
- Every recommended product is within the stated budget and matches the stated brand when one is given.
- Says plainly when a soft preference (durable, educational, age range) cannot be verified from the catalog entry.

# Fail criteria

- Recommends a product above the budget or of a different brand without labelling it as outside the constraint.
- States a rating, review, award or feature that is not in the retrieved evidence.
- Answers from memory without calling rag.search.
