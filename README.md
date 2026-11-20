# ShopVoice: agentic voice-to-voice assistant for product discovery

A voice-to-voice, multi-agent shopping assistant. You ask a question out loud; it transcribes it, plans,
retrieves grounded evidence from a private product catalog (Amazon Product Dataset 2020) through an MCP server,
optionally compares with live web results, checks its own answer, and speaks a short recommendation while the screen
shows the comparison table, citations and the full agent trace.

```
Voice -> ASR -> Router -> Planner -> Retriever (MCP: rag.search / web.search) -> Answerer <-> Critic -> TTS
                                                                  evaluated end to end with ProofAgent
```

**Start with [index.html](index.html)**: an illustrated, single-file guide (works offline, light and dark themes) with the
interactive architecture diagram, the Router's routes, three recorded runs you can step through, and the evaluation
results. `python scripts/refresh_showcase.py` re-records the runs shown in it.

| Requirement | Where |
|---|---|
| LangGraph multi-agent workflow | `src/voice_agent/graph/` |
| ASR (Whisper) / TTS | `src/voice_agent/speech/` |
| Agentic RAG, hybrid retrieval | `src/voice_agent/rag/store.py`, `graph/nodes.py` (retriever) |
| MCP server with two tools | `src/voice_agent/mcp_server/`, [docs/MCP.md](docs/MCP.md) |
| UI (Streamlit) | `src/voice_agent/ui/app.py` |
| ProofAgent evaluation | `eval/`, [eval/README.md](eval/README.md), [eval/FAILURE_ANALYSIS.md](eval/FAILURE_ANALYSIS.md) |
| Prompt disclosure | [prompts/](prompts/README.md) |
| Model-agnostic config | `.env.example`, `src/voice_agent/config.py`, `llm.py` |

## Quick start

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or plain `pip`). No system ffmpeg is needed. Tested on Windows and on macOS (Apple Silicon, Python 3.12).

```bash
# 1. environment (Windows: scripts\setup.ps1)
./scripts/setup.sh                 # venv + pip install -e ".[eval,dev]" + creates .env
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1

# 2. configure: edit .env (LLM provider/model + key; optional web search key)

# 3. data + index (downloads the public Kaggle dataset with kagglehub; about 3 minutes on CPU)
./scripts/build_index.sh           # = python -m voice_agent.data.ingest && python -m voice_agent.rag.store --build

# 4. run
./scripts/run_ui.sh                # Streamlit UI at http://localhost:8501
python -m voice_agent.pipeline "I need a wooden puzzle for a toddler under twenty dollars"
python -m voice_agent.pipeline --audio question.wav --speak
python scripts/mcp_smoke.py        # MCP discovery + one call per tool

# 5. evaluate
python -m pytest                   # 39 tests, no LLM needed
python eval/run_stage_checks.py    # stage-level checks (add --voice for TTS -> ASR -> agent -> TTS)
python eval/run_proofagent.py      # ProofAgent scorecard -> eval/results/
```

> **Windows path limit.** If the project sits in a deep folder, create the virtual environment at a short path
> (`scripts\setup.ps1 -Venv C:\venvs\shopvoice`), otherwise package installs fail at 260 characters.

### Choosing the model (model-agnostic)

Nothing in the code names a provider. Two variables select any LangChain chat model:

| `.env` | Effect |
|---|---|
| `LLM_PROVIDER=openai` `LLM_MODEL=gpt-4.1-mini` | OpenAI (`OPENAI_API_KEY`) |
| `LLM_PROVIDER=anthropic` `LLM_MODEL=claude-haiku-5-5` | Anthropic (`ANTHROPIC_API_KEY`); `claude-sonnet-5-5` for more capability |
| `LLM_PROVIDER=ollama` `LLM_MODEL=qwen3:14b` | local open-weight model, no key |
| `LLM_PROVIDER=google_genai` / `bedrock_converse` | install the matching `langchain-*` package |
| `LLM_PROVIDER=rules` | no model: every node uses its deterministic fallback (tests, offline demo) |

Structured outputs use function calling and the Retriever binds the MCP tools with `bind_tools`, so tool calling
stays enabled on every provider. One provider difference is handled in `llm.py`, not in the nodes: current Claude
models reject a forced `tool_choice`, so for Claude the schema is offered as a tool with `tool_choice=auto` and the
prompt asks for the call (`LLM_STRUCTURED_MODE=auto`); other providers force the schema tool (`forced`). No sampling
temperature is sent unless `LLM_TEMPERATURE` is set. A hosted provider with no API key is reported in the UI and
the pipeline runs on its rule-based fallbacks rather than failing. Every call has an output cap and a timeout
(`LLM_MAX_TOKENS`, `LLM_TIMEOUT_SECONDS`). ASR, TTS,
embeddings and web search are swapped the same way (`ASR_PROVIDER`, `TTS_PROVIDER`, `EMBEDDING_PROVIDER`,
`WEB_SEARCH_PROVIDER`); see [.env.example](.env.example).

## Data preprocessing and indexing

**Source.** [Amazon Product Dataset 2020](https://www.kaggle.com/datasets/promptcloud/amazon-product-dataset-2020)
(PromptCloud, 10,002 products crawled in January 2020). `voice_agent/data/ingest.py` downloads it and writes
`data/processed/products.parquet`.

**What the dataset really contains.** The brief's example (household cleaners with ratings and ingredients) does not
match this file, so the pipeline is honest about it rather than inventing fields:

| Target field | In the raw data? | What the pipeline does |
|---|---|---|
| id, title, category, price | yes | cleaned; price ranges (`$44.00 - $54.95`) keep low and high |
| features | yes (`About Product`) | boilerplate removed |
| brand | **empty column** | recovered from the packed `Product Specification` / `Technical Details` text where it exists (`Manufacturer:` 729 products, `Brand Name` 183); otherwise inferred from the leading title token(s), flagged `brand_inferred` and marked in the UI |
| recommended age (extra) | inside `Product Specification` | `Manufacturer recommended age` parsed for 5,899 products (62%), e.g. "36 months - 5 years"; this is what age-suitability statements are grounded in |
| rating, reviews | **not present** | left null; the assistant says there are no ratings and never states one |
| ingredients | **empty column** | material keywords found in the text (`wooden`, `bpa-free`, `non-toxic`, ...) |
| sku | **empty column** | ASIN parsed from the product URL |
| category mix | 67% Toys & Games; almost no household cleaning | all categories are indexed; `CATEGORY_REGEX` narrows the slice |

The empty columns were verified, not assumed: the file is 10,002 rows x 28 columns, the only version on Kaggle, and
13 of the 28 columns contain no value at all (Brand Name, Asin, Sku, Ingredients, List Price, Stock, Product
Description and six others); a search of every text column for "out of 5 stars" or review counts finds nothing. The
brief lists these fields as examples ("metadata such as", "example response", optional `brand?` / `ingredients?`),
so the schema keeps every one of them and leaves a value null where the data has none.

Curation keeps products with a title, a parseable price and descriptive text: **9,513 products**.
Unit normalisation: `price_per_oz = price / item weight` where a weight is listed (8,493 products).
There is no `reviews.parquet` because the dataset has no reviews.

**Index.** `voice_agent/rag/store.py` embeds `title + features + description + brand + category` into a persistent
Chroma collection (cosine). Embeddings default to all-MiniLM-L6-v2 running locally through onnxruntime
(`EMBEDDING_PROVIDER=local`); Ollama and OpenAI embeddings are drop-in alternatives. Metadata stored per vector:
price, brand, top category, sku, rating (when present).

**Hybrid retrieval.**
1. Metadata filters (price range, brand, category, rating) are applied to both retrievers. Numeric filters are pushed
   down into Chroma; brand matching is case-, punctuation- and `&`/`and`-insensitive, so the spoken "Melissa and
   Doug" matches the catalog's "Melissa & Doug".
2. Dense: Chroma vector search. Sparse: BM25 over the same text.
3. Reciprocal rank fusion, then a light rerank by query-term coverage of the title.
4. Each hit is labelled `relevance: strong | weak`. Vector search always returns nearest neighbours, so this label
   is how the agent tells "no such product" from a match. Only strong hits become evidence.

## LangGraph architecture

```mermaid
flowchart LR
    mic([Mic / upload]) --> asr[ASR<br/>Whisper]
    asr --> router{Router<br/>intent, constraints, safety}
    router -- unsafe / unclear / off-topic --> direct[Respond direct<br/>fixed wording]
    router -- product task --> planner[Planner<br/>sources + tool steps]
    planner --> retriever[Retriever<br/>agentic tool loop]
    retriever <--> mcp[(MCP server<br/>rag.search / web.search)]
    retriever --> answerer[Answerer<br/>picks + spoken summary]
    answerer --> critic{Critic<br/>rules + LLM}
    critic -- revise once --> answerer
    critic -- approve / repair --> finalize[Finalize<br/>caveats + citations]
    finalize --> tts[TTS]
    direct --> tts
    tts --> out([Speech + screen])
```

State is one `AgentState` TypedDict (`graph/nodes.py`); every node appends an entry to `state["steps"]`, which is
the execution log shown in the UI and written to `logs/agent_runs.jsonl`.

| Node | What it does | LLM prompt | Enforced in code |
|---|---|---|---|
| `asr` | faster-whisper (local) or OpenAI transcription; segment timestamps | none | empty transcript -> "didn't catch that" |
| `router` | task, constraints (budget, brand, material, preferences), `wants_live_info`, safety flag; resolves follow-ups from history | `router.md` | input guardrail runs first; a spoken budget found by regex is never lost |
| `planner` | chooses `rag.search` and, only for current price/availability, `web.search`; lists retrieval fields and comparison criteria; sees tools from MCP discovery | `planner.md` | always one catalog step; web step only when requested and configured; budget/brand copied into filters; max 4 steps |
| `retriever` | LLM issues MCP tool calls (up to 3 rounds); self-corrects on empty results; reconciles live and catalog data | `retriever.md` | tool allow-list; hard constraints re-applied to every call; planned steps run even if the LLM skips them; tool text sanitised |
| `answerer` | picks up to 3 products by `doc_id`, one-sentence reasons, <= 45-word spoken summary, caveats, follow-up question | `answerer.md` | title/price/rating/materials are copied from evidence by `doc_id`, never generated; unknown ids are dropped |
| `critic` | grounding, constraints, conflicts, safety, quality | `critic.md` | deterministic checks (budget, cited ids, prices and ratings not in evidence, unsupported claims) |
| `finalize` | after one failed revision, replaces the draft with an evidence-only template; adds mandatory caveats; builds citations | none | all of it |
| `tts` | speaks the summary (pyttsx3 offline or OpenAI) | none | summary capped at 45 words (about 15 s) |

**Agentic RAG behaviours** (all visible in the step log):
- *Self-correction*: no strong hit -> retry with a simpler query; rating filter impossible (no ratings in the data)
  -> drop it and say so; brand filter matches nothing -> search by name and say results may be other brands.
- *Reconciliation*: a live result is matched to a catalog product by ASIN in the URL, then brand + title similarity,
  then title similarity. A price difference above 10%, or "out of stock", is recorded as a conflict and shown with
  both values and both sources; the live price is treated as newer. If the live price exceeds the budget the product
  is not recommended and the reason is listed.
- *No match*: when nothing is strongly relevant the assistant says so and lists the closest items as non-matches.
- *Named products*: a product the user asks about by name ("check the LEGO Creative Suitcase price") is looked up
  without the budget filter and shown first with an explicit "above your budget" label when it is, next to the
  in-budget matches. An over-budget product is therefore never reported as "not in the catalog", and a request that
  mixes a search with a price check gets the same answer whichever way the Router labels it.
- *Brand is hard*: when nothing of the requested brand fits, other brands are listed as non-matches, not recommended.
- *Type and material verification*: similarity is not trusted alone. Evidence must contain the product's head noun
  ("puzzle" for "wooden puzzle for toddlers"), which removes hits that share only an adjective; and when the user
  names a material, either only products listing it are kept or the answer states that none of the options meets it.

**Repeatability.** `LLM_SEED` (default 42) is passed to providers that accept a sampling seed, temperature is 0, and
`wants_live_info` from the LLM Router is accepted only when the transcript really asks for current price or
availability, so the same request plans the same tools.

**Speech length.** The spoken summary is capped at 45 words, and because a word cap does not guarantee a duration,
the TTS node measures the synthesised audio and drops trailing sentences until it is at most 15 seconds.

**Memory.** `AgentSession` keeps the last four turns (request, constraints, products shown) so "a cheaper one" or
"under ten instead" keeps the product type and changes only what the user changed.

## MCP server

One server, two tools, discovery through `tools/list`, stdio or streamable HTTP. Full schemas, example responses,
caching, rate limiting and logging are in **[docs/MCP.md](docs/MCP.md)**.

| Tool | Purpose | Returns |
|---|---|---|
| `rag.search` | hybrid search of the private catalog with metadata filters | `{doc_id, sku, title, price, rating, brand, ingredients, category, features, relevance}` |
| `web.search` | live web search via Brave, Serper or Tavily | `{title, url, snippet, price?, availability?}` with TTL cache (180 s), rate limit, domain rules |

## User interface

`streamlit run src/voice_agent/ui/app.py`

- **Live pipeline timeline.** While a request runs, a vertical timeline shows each node as waiting, running, done,
  skipped or blocked, with its elapsed time, and a card appears as each node finishes with its intermediate result:
  the Router's route (all 8 routes shown, the chosen one highlighted) and extracted constraints, the Planner's tool
  steps, the MCP tool-call table and the evidence kept, the Answerer's draft, the Critic's verdict and issues. Built
  on `AgentSession.stream()`, which wraps LangGraph's task and update stream.
- **Guardrails are explicit.** When a request is stopped, a banner states that it was blocked, at which step, by
  which mechanism (input pattern guardrail before any LLM call, or the Router's safety classification) and in which
  category, and lists the steps that were therefore not executed; the timeline marks the blocking node and strikes
  the skipped ones. A "Guardrail checkpoints" panel lists every check in pipeline order with its outcome for this
  turn: PASSED, FLAGGED, BLOCKED, DIVERTED, ADJUSTED, SANITISED, NOTED, REJECTED, REPAIRED or NOT REACHED. The data
  comes from structured `guard` records each node writes into the step log (`graph/nodes.py`, `_guard`).
- Recommendation cards (price, live price, age, materials, over-budget flag, citation), with the comparison table,
  notes and citations in tabs next to them.
- Microphone recording (`st.audio_input`), file upload (WAV/MP3/M4A), or typed text; example requests.
- Transcript with ASR segment timestamps.
- Spoken answer with audio playback (autoplay) and the critic's verdict.
- Comparison table: Product | Price (catalog) | Live price | Rating | Ingredients/materials | Recommended age | Brand | Why | Source | Citation (doc_id) | Live source link.
- Notes and conflicts, citations (private doc ids and live URLs), unmatched live results.
- "Evaluation evidence" tab: ProofAgent scorecard with per-metric bars, stage-level results and the failure analysis.
- Header: the active LLM, ASR, TTS, web-search and MCP providers. Sidebar (collapsed): TTS toggle, new conversation,
  the LangGraph diagram. Theme in `.streamlit/config.toml`.

## Safety considerations

| Risk | Control |
|---|---|
| Unsafe chemical/product advice, harm | Regex guardrail before any LLM call (`safety.check_input`) plus the Router's safety flag; fixed refusal with a short safety note; no tools are called |
| Prompt injection from the user | Same guardrail; fixed reply; instructions never revealed |
| Prompt injection from tool results | Instruction-like text is stripped from catalog and web text, the result is flagged, excluded from matching and never cited; prompts state that tool output is data |
| Hallucinated products, prices, ratings | Product fields are copied from evidence by `doc_id`; the critic blocks prices and ratings that are not in evidence |
| Unsupported claims | Pattern checks for germ-kill, medical, certification, absolute-safety and best-seller claims that the evidence does not contain |
| Over-budget recommendations | Budget is re-applied to every `rag.search` call and re-checked by the critic |
| Tool misuse | Client-side allow-list of the two tools; `web.search` only when the plan includes it; max 6 calls per turn; domain allow/block lists |
| Secrets | Keys only in `.env` (git-ignored); every log line and every output passes `redact()` |
| Stale data presented as current | Every answer states that prices are a January 2020 snapshot unless a live price is shown |

## Evaluation

Methodology, scenarios and how to reproduce: **[eval/README.md](eval/README.md)**. Results and the analysis of
failures: **[eval/FAILURE_ANALYSIS.md](eval/FAILURE_ANALYSIS.md)**.

Three layers, all run against the complete pipeline:
1. `pytest` (39 tests): units plus end-to-end runs through the real MCP server with the LLM disabled.
2. `eval/run_stage_checks.py`: 20 scenarios whose expectations are attributed to a stage
   (ASR, Router, Planner, RAG/MCP, Answerer, TTS), in text mode and in voice mode.
3. `eval/run_proofagent.py`: the ProofAgent harness (`proofagent-harness`) with 12 project-specific adversarial
   scenarios in `eval/proofagent/traps/`, jury scoring on task success, hallucination resistance, tool use, safety,
   manipulation resistance and instruction following.

Results with `claude-haiku-5-5` as the agent LLM (details, all four runs and the failure cases in
[eval/FAILURE_ANALYSIS.md](eval/FAILURE_ANALYSIS.md)):

| Layer | Result |
|---|---|
| Tests | 39 / 39 |
| Stage checks, text | 172 / 172 |
| Stage checks, voice (TTS -> ASR -> agent -> TTS) | 211 / 212 (one ASR miss on disfluent speech) |
| ProofAgent, best complete run (run 2) | 8.94 / 10, certification SILVER |
| ProofAgent, latest run (run 4) | 7.78 / 10, NEEDS_ENHANCEMENT: five metrics at 8.1-9.4, Task Success cut from 9.12 to 2.74 by the harness's repeat-consistency rule |

The ProofAgent headline varied by more than a point between runs; the two defects behind the latest drop (one
answer per multi-part request, and over-blocking of requests that quote injected text) were fixed afterwards and
have not been re-scored.

## Logs

| File | Content |
|---|---|
| `logs/prompts.jsonl` | every rendered system/user prompt per node, model, structured output, token usage, latency |
| `logs/mcp_tool_calls.jsonl` | every MCP request/response: timestamp, arguments, result ids or URLs, cache hit, latency |
| `logs/agent_runs.jsonl` | one record per turn: transcript, intent, plan, tool calls, conflicts, citations, critic verdict |

## Running cost

Measured from the token usage in `logs/prompts.jsonl` on nine requests with `claude-haiku-5-5` (effort `low`) at
its list price of $0.10 per million input tokens and $0.50 per million output tokens. No prompt exceeded 6,300
tokens. ASR (faster-whisper) and TTS (pyttsx3) run locally and cost nothing.

| Request | LLM calls | Input tokens | Output tokens | LLM cost |
|---|---|---|---|---|
| Blocked or out of scope (Router only) | 1 | 3,600 | 130-180 | $0.0004 |
| Recommendation or comparison | 6 | 22,900-23,400 | 2,800-3,900 | $0.004 |
| Price check with live search | 6-7 | 26,500-29,200 | 2,700-3,700 | $0.004-0.005 |
| Draft sent back once by the Critic | 8 | 25,700-33,300 | 3,100-5,400 | $0.004-0.006 |
| Average over the nine requests | 5.6 | 21,800 | 2,740 | $0.0036 |

A full request is about 0.4 cents: one Router call, one Planner call, two or three Retriever tool rounds, one
Answerer call and one Critic call. Each uncached `web.search` call uses one Tavily credit (1,000 free credits per
month; $0.008 per credit after that), and a price check makes one or two. One ProofAgent evaluation run cost
$0.3-1.4, most of it the stronger fallback judge.

## Limitations

- The 2020 dataset has no ratings, reviews, brand or ingredient fields, and little outside toys and games; the
  brief's cleaner example returns "no match" by design.
- Relevance labelling uses similarity thresholds calibrated for the default MiniLM embeddings
  (`RAG_STRONG_SIM`, `RAG_WEAK_SIM`); another embedding model needs recalibration.
- Live price extraction parses search snippets, so it depends on what the search API returns: the same price
  check returned no price on one run and a price from two shops on the next. Shops are not vetted; the answer
  cites the URL so the user can check it.
- Fragment-based speech (record, then transcribe); no streaming or barge-in.
- Input guardrails are English regex patterns backed by the Router LLM; they are a floor, not a complete filter.
- Answers to an identical request can differ in wording (Claude models take no sampling seed); picks, prices and
  constraint handling are kept stable in code, but the ProofAgent consistency probe still penalised this.
- The evaluation judge was a small model with a stronger fallback; its scores moved by more than a point between runs.

## Project layout

```
src/voice_agent/
  config.py  llm.py  schemas.py  safety.py  logging_utils.py  mcp_client.py  pipeline.py
  data/ingest.py            raw CSV -> products.parquet
  rag/store.py              Chroma + BM25 hybrid search, index builder
  mcp_server/server.py      MCP server (rag.search, web.search);  web.py  providers, cache, rate limit
  graph/build.py nodes.py rules.py reconcile.py     LangGraph workflow
  speech/asr.py tts.py
  ui/app.py                 Streamlit
prompts/                    all prompts + mapping to nodes
eval/                       ProofAgent config, scenarios, runners, results, failure analysis
docs/MCP.md                 tool schemas
scripts/                    setup, build_index, run_mcp, run_ui, run_eval (.sh and .ps1), mcp_smoke.py
tests/                      pytest
```
