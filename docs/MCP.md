# MCP server: `product-discovery`

One server (`src/voice_agent/mcp_server/server.py`, built on the official `mcp` Python SDK) exposing two tools.

| Transport | Start | Used by |
|---|---|---|
| stdio (default) | `python -m voice_agent.mcp_server.server` | the agent spawns it as a subprocess (`MCP_TRANSPORT=stdio`) |
| streamable HTTP | `python -m voice_agent.mcp_server.server --transport http --port 8765` | `MCP_TRANSPORT=http`, `MCP_URL=http://127.0.0.1:8765/mcp`; also usable from Claude Desktop, MCP Inspector, etc. |

**Discovery.** Clients call `tools/list`; the server returns both tools with names, descriptions and JSON schemas.
The Planner node receives this list on every run, and `scripts/mcp_smoke.py` prints it.

## `rag.search`

Hybrid retrieval over the private catalog: Chroma vector search (cosine) + BM25, both restricted by the metadata
filters, fused with reciprocal rank fusion and reranked by query-term coverage of the title.

Input schema:

```json
{
  "type": "object",
  "required": ["query"],
  "properties": {
    "query":      {"type": "string",  "description": "What to look for, e.g. 'wooden puzzle for toddlers'"},
    "top_k":      {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
    "max_price":  {"anyOf": [{"type": "number"}, {"type": "null"}], "description": "Budget ceiling in USD (inclusive)"},
    "min_price":  {"anyOf": [{"type": "number"}, {"type": "null"}], "description": "Price floor in USD (inclusive)"},
    "brand":      {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Brand name filter (case-insensitive)"},
    "category":   {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "Category substring, e.g. 'Toys & Games'"},
    "min_rating": {"anyOf": [{"type": "number"}, {"type": "null"}], "description": "Minimum star rating (0-5)"}
  }
}
```

Response:

```json
{
  "results": [{
    "doc_id": "amz20-7bf4d392c47b",        "sku": "B000B9RHYC",
    "title": "Melissa & Doug Safari Wooden Chunky Puzzle ...",
    "price": 9.99, "price_max": null,      "rating": null,
    "brand": "Melissa & Doug", "brand_inferred": true, "brand_source": "title",
    "recommended_age": "24 months - 4 years", "age_min_years": 2.0,
    "ingredients": "wooden",               "category": "Toys & Games > Puzzles > Pegged Puzzles",
    "features": "8-PIECE WOODEN PUZZLE: ...", "price_per_oz": 0.62,
    "url": "https://www.amazon.com/...",   "score": 0.04416, "similarity": 0.6746,
    "relevance": "strong",                 "source": "amazon-product-dataset-2020"
  }],
  "meta": {"count": 1, "index": "amazon-product-dataset-2020"}
}
```

`doc_id` is the citation id. `relevance` (`strong` / `weak`) tells the agent whether a hit is confidently the kind of
product asked for; only `strong` hits become evidence, which is how "no such product" is detected.

## `web.search`

Wraps a real search API and normalises the output. Provider is `WEB_SEARCH_PROVIDER`: `brave` (Brave Search API),
`serper` (Serper.dev), `tavily` (Tavily), `fixture` (canned test data), or `none`.

Input schema:

```json
{
  "type": "object",
  "required": ["query"],
  "properties": {
    "query":       {"type": "string",  "description": "Search query, ideally brand + product name + 'price'"},
    "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10}
  }
}
```

Response:

```json
{
  "results": [{
    "title": "...", "url": "https://...", "snippet": "...",
    "price": 13.49,              "availability": "in_stock",
    "source_domain": "example.com", "suspicious": false
  }],
  "meta": {"provider": "brave", "cache_hit": false, "query": "..."},
  "error": "present only on failure, e.g. rate limit reached"
}
```

- `price` is taken from the provider when it supplies one, otherwise parsed from the title/snippet; `availability`
  is parsed from stock phrases. Both are optional. Snippets often contain several dollar amounts, so amounts that
  are shipping thresholds, discounts or coupons ("free shipping over $75", "$5 off") are skipped and an amount
  introduced by "price", "now", "sale" or "only" is preferred.
- **Several shops per product**: the agent keeps every result that matches a catalog product, reports the lowest
  plausible live price (in-stock shops first) with its URL, and gives the range when shops disagree. A live price
  more than 4x or less than 1/4 of the catalog price is treated as a different listing and not used.
- **Verified live**: the tool was run end to end against the Tavily API (`search_depth: basic`, one API credit
  per uncached call). Unit tests, stage checks and the ProofAgent runs use `fixture` so that results are
  repeatable; live results differ from call to call.
- **Caching**: in-memory TTL cache keyed by provider + query + size, `WEB_CACHE_TTL_SECONDS` (default 180).
- **Rate limiting**: sliding window, `WEB_RATE_LIMIT_PER_MIN` (default 20). Short waits are absorbed; longer ones
  return an `error` so the agent can answer from the catalog.
- **Domain restrictions**: `WEB_ALLOWED_DOMAINS` (allow-list) and `WEB_BLOCKED_DOMAINS`; only http/https URLs pass.
- **Prompt-injection hygiene**: instruction-like text in titles/snippets is removed and the result is marked
  `suspicious`; the agent never matches or cites suspicious results.
- **Terms of use**: only official search APIs are called, with the user's own key; no scraping of retailer pages.

## Logging

Every call appends one line to `logs/mcp_tool_calls.jsonl` (secrets redacted):

```json
{"ts": "2026-11-19T20:26:51.611+00:00", "tool": "web.search",
 "request": {"query": "LEGO Classic Creative Suitcase 10713 Building Kit (213 Pieces) price", "max_results": 5},
 "response": {"count": 2, "cache_hit": false, "error": null},
 "sources": ["https://bricks.retailer.test/p/lego-classic-creative-suitcase-10713", "https://deals.spam.test/lego"],
 "provider": "fixture", "latency_ms": 0.3}
```

The agent side records the same calls (arguments after constraint enforcement, origin `llm` / `plan` / `relaxation`,
result ids, latency) in the UI's execution log and in `logs/agent_runs.jsonl`.

## Client-side policy

`src/voice_agent/mcp_client.py` keeps one session on a background thread and enforces a tool allow-list
(`rag.search`, `web.search`); any other tool name is rejected before reaching the server. Because LLM APIs do not
allow dots in function names, tools are presented to the model as `rag_search` / `web_search` and mapped back.

**Windows note.** With stdio transport the server loads the index and embedding model *before* it starts reading
stdin. Loading them lazily inside the first tool call can block forever on Windows, because libraries that probe the
inherited stdin pipe wait on the transport's pending read.
