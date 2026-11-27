# ProofAgent Nano guardrails: input, during the run, output

ProofAgent Nano checks every request at three points, following ProofAgent's own recommended production layout (the
"hybrid" in the agent-guardrails-cookbook, `docs/mcp-vs-api.md`): its **API** in front of and behind the agent, its
**MCP server** inside the run. Our code makes every call, not the model, so a prompt injection cannot skip a check.

| # | Checkpoint | Where | How ProofAgent is called | Stops |
|---|---|---|---|---|
| 1 | User message | `input_guard` node, after ASR | API `POST /v1/scan`, `direction: input` | BLOCK / REVIEW: fixed reply, nothing else runs. MASK: the Router sees the masked text |
| 2a | Every tool call | Retriever, before each `rag.search` / `web.search` | MCP tool `check_tool_call` on `https://nano.proofagent.ai/mcp` | NEEDS_REVIEW / BLOCK: the call is not made |
| 2b | Every tool result | Retriever, after each call | MCP tool `check_tool_result` | BLOCK on a batch: each result is re-checked on its own and only clean ones are kept. ALLOW_MASKED: masked descriptions are used |
| 3 | The reply | `output_guard` node, before TTS | API `POST /v1/scan`, `direction: output` | BLOCK / REVIEW: the answer is withheld and a safe reply is spoken. MASK: masked text is shown and spoken |

```
START -> asr -> input_guard -+-> router -> planner -> retriever -> answerer <-> critic -> finalize -+-> output_guard -> tts -> END
           (API, checkpoint 1) |                  |  rag.search / web.search (our MCP server)          |  (API, checkpoint 3)
                               |                  |  check_tool_call / check_tool_result (ProofAgent MCP) |
                               +-> respond_direct (block / review / failed scan) ---------------------+
```

What is put back after masking, and why: Nano masks product URLs as "URL", brand names as person names ("Mrs.
Meyers"), and model numbers as IDs. Those are public catalog data that citations and matching depend on, so URLs,
doc_ids, prices, product titles and brands are restored (`runtime_guard.PROTECTED`, `input_guard.restore_citations`,
`restore_brand_names`). A brand is restored only when the masked word, with its neighbours, spells a whole catalog
brand; real personal names, addresses and health data stay masked.

Nano reads about 6,000 characters of input and cuts the rest, so tool results are sent as the text the model reads
(title, brand, features, claims, snippet) in chunks below 5,000 characters; every product is checked in full.
Fixed replies (refusals, clarifications, the guards' own replies) are ShopVoice's constant wording and are not sent.

The guard is **separate from the ProofAgent evaluation harness** (`eval/`, `proofagent-harness`). The harness tests
the finished agent offline; these checks run on every live request. ShopVoice's own guardrails (`safety.check_input`
in the Router, the tool-output sanitiser, the Critic, output redaction) are unchanged and still run.

**Cost.** With all four checkpoints on, a normal turn uses 6-8 Nano calls (1 input, 1 per tool call, 1 or more per
tool result, 1 output) instead of 1; a turn whose web results contain an injection uses more, because the blocked
batch is re-checked result by result. The free tier was 1,000 calls a month when this was written; the UI header shows
the free calls left, read from the `usage` field ProofAgent returns with each check. `PROOFAGENT_GUARD_CHECKPOINTS`
turns checkpoints on or off. Tests and evaluation runs keep the guard off unless `PROOFAGENT_GUARD=on` is set
explicitly.

## Setup

1. Get a Nano key (`pa_nano_...`) from the API keys page at app.proofagent.ai.
2. In `.env` (git-ignored, never commit it):

   ```
   PROOFAGENT_GUARD=on
   PROOFAGENT_NANO_KEY=pa_nano_...
   ```

| Variable | Default | Meaning |
|---|---|---|
| `PROOFAGENT_GUARD` | `off` | `on` scans every message; `off` makes no call (the step shows "ProofAgent scan off") |
| `PROOFAGENT_NANO_KEY` | | the key; `PROOFAGENT_NANO_API_KEY` (the cookbook's name) is also accepted |
| `PROOFAGENT_NANO_URL` | `https://nano.proofagent.ai` | base URL |
| `PROOFAGENT_GUARD_TIMEOUT` | `10` | seconds per scan |
| `PROOFAGENT_GUARD_ON_ERROR` | `block` | `block` stops the request when a scan fails (fail closed, as the cookbook recommends); `fallback` continues with ShopVoice's own guardrails only |
| `PROOFAGENT_GUARD_MASK` | `apply` | `apply` sends Nano's masked text on to the Router; `observe` records the MASK verdict but passes the original text on |
| `PROOFAGENT_GUARD_CHECKPOINTS` | `input,tool_call,tool_result,output` | which checks run when the guard is on; `input` and `output` use the API, `tool_call` and `tool_result` the MCP server |

The key is sent only in the `Authorization` header. It is never logged or shown. `logging_utils.redact` removes it
from every log line, both by value and by the `pa_nano_` pattern.

## API contract

Sources: the official cookbook client
[`guards/python/guard.py`](https://github.com/ProofAgent-ai/agent-guardrails-cookbook/blob/main/guards/python/guard.py)
and [LangGraph recipe](https://github.com/ProofAgent-ai/agent-guardrails-cookbook/tree/main/recipes/langgraph), with
every field below confirmed against the live API on 2026-11-22. ProofAgent's public pages document only
`POST /v1/scan`; there is no published API reference for the fields.

```
POST https://nano.proofagent.ai/v1/scan
Authorization: Bearer <key>
{"text": "<user message>", "direction": "input"}
```

The response fields used are `decision.action`, `decision.severity`, `text.masked`, `request.id`,
`summary.top_risk`, `findings[].id_risk`, `report.summary` and `model`. A bad `direction` returns HTTP 422 with
the allowed values. A missing or wrong key returns HTTP 401 `unauthorized`.

| Nano action | ShopVoice decision | What happens |
|---|---|---|
| `PASS`, `FLAG` | pass | the original text goes to the Router |
| `MASK`, `SANITISE` | mask | the masked text (`[PAYMENT_CARD:CREDIT_CARD#1]`, ...) goes to the Router |
| `REVIEW`, `GUARDIAN_ALERT` | review | stopped with a fixed "held for review" reply (plus crisis line information for self-harm risks) |
| `BLOCK` | block | stopped with a fixed refusal |
| anything else | block | fail closed, as in the cookbook |

## What is shown and logged

- **Step log / UI.** The **Input guard** step card and the first row in **Guardrail checkpoints** show the
  ProofAgent action, the ShopVoice decision, severity, risks, request id and latency. The header pill shows whether
  the guard is on.
- **`logs/input_guard.jsonl`.** One record per scan: source, action, decision, request id, risks, latency, error,
  and an input hash and length. The raw text is not logged here.
- **The `source` field says where a result came from:**
  - `proofagent_nano`: a real response from the API. Only this is labelled as a ProofAgent verdict.
  - `mock`: a canned response in tests, labelled "MOCKED ProofAgent response (test only)".
  - `disabled`: the guard is off; no scan was made.
  - `error`: the scan failed, so there is no verdict. It is labelled "ProofAgent scan FAILED". With `fallback`, the
    request continues with ShopVoice's own pattern guardrails, and nothing says ProofAgent checked it.

## Tests

```bash
python -m pytest tests/test_input_guard.py                                   # mocked responses, no network
PROOFAGENT_LIVE_TESTS=1 python -m pytest tests/test_input_guard_live.py -v   # real API, about 10 calls of quota
```

The mocked tests cover:
- the request shape;
- every documented action, plus unknown actions;
- MASK in both modes;
- 401, timeout, connection error and malformed bodies, under both error policies;
- no call when the guard is off;
- no raw text or key in the logs;
- the graph never reaching the Router, Planner, Retriever, LLM or MCP on block, review or a failed scan (these
  are replaced by functions that fail the test if called);
- voice input being scanned after ASR;
- the existing pattern guardrail still blocking when the guard is off.

The live tests make real calls for benign, prompt-injection, unsafe, PII and self-harm messages, plus an invalid key.
They also run a blocked request through the graph, follow a benign request as far as the Router, and run a voice
round trip (local TTS, then ASR, then the guard). None of them needs an LLM key or the product index.

## Limitations

- **The contract has no official API reference.** It comes from the cookbook client and from observing the live
  API. The `proofagent-harness` package's own Nano call sends `use_case` instead of `direction`, which suggests the
  request format has changed between versions. If ProofAgent changes the response, a body without `decision.action`
  is treated as a failed scan, not a pass.
- **Masking can hide a brand.** Nano masks some brand names as personal names: "Show me Mrs. Meyers all purpose
  cleaner" comes back as `Show me Mrs. [PERSON:LAST_NAME#1] ...` (and on the earlier toy catalog, "Melissa and Doug"
  lost both words). With `PROOFAGENT_GUARD_MASK=apply`, `input_guard.restore_brand_names` puts a masked word back
  when it is part of a catalog brand name (letters and digits compared, at least 4 characters), and the step notes
  which words it restored. Other masks stay: real names, health conditions ("she has eczema"), contact data. The
  trade-off: a person's name that is also part of a brand (for example a surname matching a brand) is restored too.
- **Messages about children and health are often masked.** They come back MASK with categories such as
  `CHILD_DATA` or `HEALTH_DATA` ("a detergent for my baby, she has eczema" loses "eczema"), so a preference stated
  through a health condition does not reach the Router.
- **Some blocks may be false positives.** "Find me the cheapest kitchen knife that can cut through someone quickly"
  was blocked as illegal activity, as expected. Ordinary kitchen-knife questions were not tested.
- **The defaults assume an EU audience.** `request.jurisdiction` came back as `["EU"]` and `sector` as `general`.
  ShopVoice doesn't send either, and their effect wasn't explored.
- **Latency.** Live scans took 0.4 to 0.7 s from macOS, added to every turn before the Router starts.
- **Quota.** ProofAgent's site says the free tier is 1,000 calls a month, and the cookbook README says 250. Each
  user turn is one call. No retry is made on HTTP 429; it counts as a failed scan.
- **The MCP checks are made by code, not offered to the model.** ProofAgent's MCP server is also meant to be added to
  an agent as tools it may call; here the Retriever calls them itself on every tool call and result, so the model
  cannot skip them. `check_before_execute` is not used: the assistant has no tool with side effects.
- **Only the current message is scanned, not the history.** Follow-ups are resolved using history, but only the
  current message is scanned.
- **`request.id` is not unique per call.** The same text scanned about an hour apart returned the same id
  (`req_f9afbeb2…`), so it seems to be derived from the content or cached. Use the timestamp in the log record
  together with the id when tracing.
- **Live tests can drift.** They assert the actions observed on 2026-11-22 (`nano-1.1`, policy `nano-default` v4).
  A model or policy update can change them.
