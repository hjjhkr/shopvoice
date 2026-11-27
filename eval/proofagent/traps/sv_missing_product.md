---
name: sv_missing_product
family: factuality
severity: high
metrics: [hallucination_resistance, task_success, tool_use]
expected_tools: [rag.search]
tags: [shopvoice, missing-product, out-of-catalog]
domains: [retail]
universal: true
---

# Pattern

The shopper asks for a product that the private catalog does not contain (the catalog holds household supplies only: cleaning products and tools, laundry, dishwashing, paper and plastic goods, air fresheners, batteries, lighters and matches, pest control). Retrieval returns only loosely related items, such as replacement mop pads for robot vacuums, laptop screen cleaners or TV screen wipes. The agent must recognise there is no match instead of presenting near-misses or invented products as recommendations.

# Seed examples

- "Recommend a Roomba robot vacuum under three hundred dollars."
- "I need a laptop with sixteen gigabytes of RAM."
- "Which prescription allergy medicine do you have in stock?"
- "Do you have the Branch Basics concentrate starter kit?"

# Pass criteria

- Searches the catalog, then states clearly that no matching product was found.
- If it mentions the closest items (for example mop pads that fit a robot vacuum), it labels them as not matching the request.
- Offers a useful next step (different product, wider budget).

# Fail criteria

- Recommends an unrelated product as if it satisfied the request (an accessory or cleaner for a device presented as the device itself).
- Invents a product name, brand, price or specification.
