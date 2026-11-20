# Seven-minute demo script

Start the UI (`scripts/run_ui`) with the index built. Keep the "Agent execution log" expander open.

| Time | Say or click | What to point at |
|---|---|---|
| 0:00 | One-sentence problem statement and the pipeline diagram (sidebar -> LangGraph flow) | Voice -> ASR -> Router -> Planner -> MCP tools -> Answerer/Critic -> TTS |
| 0:45 | **Speak:** "I need a wooden puzzle for a toddler under twenty dollars." | Transcript with ASR timestamps; router constraints (`max_price: 20`); one `rag.search` call with the budget as a filter; comparison table with doc_id citations; audio answer under 15 s |
| 2:00 | **Speak:** "Do you have a cheaper one, under ten dollars?" | Follow-up keeps "wooden puzzle" and changes only the budget (conversation memory) |
| 2:45 | **Speak:** "What's the current price of the Melissa and Doug safari chunky puzzle?" | Planner adds `web.search`; reconciliation matched by ASIN; conflict note with both prices and both sources; live source link column |
| 3:45 | **Speak:** "Recommend an eco-friendly stainless steel cleaner under fifteen dollars." | The brief's own example is not in this dataset: the agent says so instead of inventing a product, and lists near-misses as non-matches |
| 4:30 | **Click:** "Which cleaners can I mix with bleach to make it stronger?" | Guardrail blocks before any LLM or tool call; safety note |
| 5:00 | "Evaluation evidence" tab | ProofAgent scorecard, stage-level table, failure analysis |
| 6:00 | Limitations and next steps | Dataset has no ratings/brand/ingredients; relevance thresholds; stronger judge model for the final scorecard |

Fallbacks if the microphone misbehaves: upload a WAV, or click the example buttons (same pipeline, ASR skipped).

MCP side demo (optional, 30 s): `python scripts/mcp_smoke.py` prints tool discovery with both JSON schemas and
one call per tool; `logs/mcp_tool_calls.jsonl` shows the audit log.
