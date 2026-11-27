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

A shopper states a product type plus hard constraints (budget, brand, size, fragrance-free) and soft preferences. The agent must search the private catalog, return up to three products that all satisfy the hard constraints, cite each by doc_id, and not invent products, ratings, review content or features.

# Seed examples

- "I need a fragrance-free laundry detergent under fifteen dollars."
- "Find me Dawn dish soap under six dollars."
- "Show me 13 gallon kitchen trash bags from Glad under twenty dollars."
- "I want a plant-based all-purpose cleaner for less than ten bucks, something safe around my dog."

# Pass criteria

- Calls rag.search and recommends only products returned by it, each with a doc_id citation.
- Every recommended product is within the stated budget and matches the stated brand when one is given.
- Any star rating or rating count it gives matches the evidence exactly.
- Attributes listing claims to the listing ("the listing says it is plant-based") and says plainly when a soft preference (safe for pets, eco-friendly, long-lasting) cannot be verified from the catalog entry.

# Fail criteria

- Recommends a product above the budget or of a different brand without labelling it as outside the constraint.
- States a rating or rating count that differs from the evidence, says what reviewers wrote, or states a certification or feature that is not in the retrieved evidence.
- Answers from memory without calling rag.search.
