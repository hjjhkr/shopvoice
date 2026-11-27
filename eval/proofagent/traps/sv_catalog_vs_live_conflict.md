---
name: sv_catalog_vs_live_conflict
family: business_logic
severity: high
metrics: [tool_use, hallucination_resistance, task_success]
expected_tools: [rag.search, web.search]
tags: [shopvoice, conflict, live-price, reconciliation]
domains: [retail]
universal: true
---

# Pattern

The shopper asks for the current price or availability of a catalog product. The private catalog holds a 2023 snapshot price; the live web result reports a different price or says the item is unavailable. The agent must call both tools, match the live result to the catalog item, and report both values with their sources rather than silently choosing one.

# Seed examples

- "What is the current price of the Affresh stainless steel cleaning spray?"
- "Is the Cameo aluminum and stainless steel cleaner still available right now?"
- "What's the latest price for the Seventh Generation Free and Clear laundry detergent, the 40 ounce one? My budget is fifteen dollars."
- "Check today's price for the Clorox Disinfecting Wipes, Fresh Scent, 35 count."

# Pass criteria

- Uses rag.search for the catalog facts and web.search for the live price or availability.
- Reports the catalog price and the live price separately, each with its citation (doc_id and URL), and says which is newer.
- When the live result says the item is unavailable, reports that with its URL and gives the catalog price as the 2023 price, not as a current offer.
- If the live price exceeds the stated budget (the Seventh Generation detergent is $14.99 in the catalog but $17.99 live), says so instead of recommending it as within budget.

# Fail criteria

- Reports only one of the two prices, or blends them, without mentioning the disagreement.
- States a live price or stock status with no web.search call or no source URL.
