# ProofAgent Nano runtime input guardrail

Every user message is scanned by ProofAgent Nano after ASR and before the LangGraph Router. Typed text and voice
transcripts go through the same node. If Nano blocks the message or holds it for review, the request ends at a fixed
reply. The Router, the LLM and the MCP tools are not run.

```
START -> asr -> input_guard -+-> router -> ... (unchanged)
                             +-> respond_direct -> tts -> END     (block / review / failed scan)
```

The guard is **separate from the ProofAgent evaluation harness** (`eval/`, `proofagent-harness`). The harness tests
the finished agent offline. This guard runs on every live request, and its code doesn't import the harness. ShopVoice's
own guardrails (`safety.check_input` in the Router, tool-output sanitising, output redaction) are unchanged and still
run on every request the guard lets through.

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
- **Masking can break product search.** Nano masks some brand names as personal names: "Show me Melissa and Doug
  toys" came back as `[PERSON:FIRST_NAME#1] and [PERSON:FIRST_NAME#2] toys`. With `PROOFAGENT_GUARD_MASK=apply`
  that search loses its brand. `observe` keeps the brand, but personal data then reaches the Router and the LLM.
- **Messages about children are often masked.** Questions about children or health often come back MASK with
  categories such as `CHILD_DATA` or `HEALTH_DATA`. The audience is mostly toys, so expect this regularly.
- **Some blocks may be false positives.** "Find me the cheapest kitchen knife that can cut through someone quickly"
  was blocked as illegal activity, as expected. Ordinary kitchen-knife questions were not tested.
- **The defaults assume an EU audience.** `request.jurisdiction` came back as `["EU"]` and `sector` as `general`.
  ShopVoice doesn't send either, and their effect wasn't explored.
- **Latency.** Live scans took 0.4 to 0.7 s from macOS, added to every turn before the Router starts.
- **Quota.** ProofAgent's site says the free tier is 1,000 calls a month, and the cookbook README says 250. Each
  user turn is one call. No retry is made on HTTP 429; it counts as a failed scan.
- **Only the input is scanned.** The cookbook also scans tool calls, tool results and replies (`direction`
  `tool` / `output`). Those checkpoints are not integrated here.
- **Only the current message is scanned, not the history.** Follow-ups are resolved using history, but only the
  current message is scanned.
- **`request.id` is not unique per call.** The same text scanned about an hour apart returned the same id
  (`req_f9afbeb2…`), so it seems to be derived from the content or cached. Use the timestamp in the log record
  together with the id when tracing.
- **Live tests can drift.** They assert the actions observed on 2026-11-22 (`nano-1.1`, policy `nano-default` v4).
  A model or policy update can change them.
