# Seven-minute demo script

Start the UI (`scripts/run_ui`) with the index built. Keep the "Agent execution log" expander open.

| Time | Say or click | What to point at |
|---|---|---|
| 0:00 | One-sentence problem statement and the pipeline diagram (sidebar -> LangGraph flow) | Voice -> ASR -> Router -> Planner -> MCP tools -> Answerer/Critic -> TTS |
| 0:45 | **Speak:** "Recommend an eco-friendly stainless steel cleaner under fifteen dollars." | The brief's own example: router keeps "stainless steel" as the surface, not a material filter; one `rag.search` call with the budget as a filter; picks with star ratings and rating counts, the listing's own claims ("the listing says plant-based"), doc_id citations; audio answer under 15 s |
| 2:00 | **Speak:** "Do you have a cheaper one, under ten dollars?" | Follow-up keeps the stainless steel cleaner and changes only the budget (conversation memory) |
| 2:30 | **Speak:** "Find a fragrance free laundry detergent rated at least four and a half stars under twenty dollars." | Rating floor passed to `rag.search` as `min_rating: 4.5`; comparison table with rating, rating count and unit price |
| 3:15 | **Speak:** "What's the current price of the Easy-Off heavy duty oven cleaner?" | Planner adds `web.search`; live offers matched to the catalog product; conflict note with the 2023 catalog price, the live price and both sources |
| 4:15 | **Click:** "Which cleaners can I mix with bleach to make it stronger?" | Guardrail blocks before any LLM or tool call; safety note |
| 5:00 | "Evaluation evidence" tab | ProofAgent scorecard, stage-level table, failure analysis |
| 6:00 | Limitations and next steps | Ingredients are rarely listed; listing claims are unverified seller wording; no review text; relevance thresholds |

Fallbacks if the microphone misbehaves: upload a WAV, or click the example buttons (same pipeline, ASR skipped).

MCP side demo (optional, 30 s): `python scripts/mcp_smoke.py` prints tool discovery with both JSON schemas and
one call per tool; `logs/mcp_tool_calls.jsonl` shows the audit log.
