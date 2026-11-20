# Evaluation

The evaluation targets the **complete agent** (the same LangGraph pipeline the UI runs, through the real MCP
server), not the underlying LLM. Four layers answer four different questions.

| Layer | Question | Command | Output |
|---|---|---|---|
| Unit + pipeline tests | Does each component do what it claims? | `python -m pytest` | 47 tests |
| Retrieval benchmark | How good is `rag.search`, and does each part of the hybrid earn its place? | `python eval/run_retrieval_eval.py` | `eval/results/retrieval_eval.{md,json}` |
| Stage-level checks | *Where* in ASR -> Router -> Planner -> RAG/MCP -> Answerer -> TTS does a scenario fail? | `python eval/run_stage_checks.py [--voice]` | `eval/results/stage_checks*.json` |
| ProofAgent harness | How does the agent hold up in a multi-turn adversarial conversation, as judged by a jury? | `python eval/run_proofagent.py` | `eval/results/proofagent_*.json/.md` |

## ProofAgent

[`proofagent-harness`](https://pypi.org/project/proofagent-harness/) runs a multi-turn adversarial conversation
against a callable and scores the transcript with a jury of LLM jurors.

**Integration** ([proofagent/agent.py](proofagent/agent.py)). `agent(message) -> AgentResponse` runs one turn of the
pipeline with conversational memory and returns, besides the text, the MCP `tools_called` (name, arguments, result
ids) and the `retrievals` (doc_id + evidence text), so the jury scores tool use and grounding against what was
actually retrieved. With `--voice`, each attacker message is first spoken with TTS and transcribed with ASR, so the
voice path is inside the evaluated system.

**Configuration** ([proofagent/config.yaml](proofagent/config.yaml)).

| Setting | Value |
|---|---|
| Harness LLM (attacker + jury) | `PROOFAGENT_LLM`, any LiteLLM target; independent of the agent's LLM. `PROOFAGENT_FALLBACK_LLM` (optional) retries juror calls whose reply the primary judge returned as invalid JSON |
| Turns | 12, one per pinned project scenario |
| Seed | 42 (pins trap selection) |
| Consensus | independent (one blind jury round) |
| Agent context | [proofagent/context/](proofagent/context/): `agent.yaml` (role, goal, business case), `system_prompt.md` and `tools.json` regenerated on every run from `prompts/` and live MCP discovery |
| Ground truth | [proofagent/knowledge/policies.md](proofagent/knowledge/policies.md): what the catalog contains, what the agent may do, and the fixture values |
| Live data during evaluation | `WEB_SEARCH_PROVIDER=fixture` ([fixtures/web_fixture.json](fixtures/web_fixture.json)) so conflicting and poisoned web results are reproducible |

**Metrics.** The brief's dimensions map to the harness's canonical metrics:

| Brief | ProofAgent metric |
|---|---|
| Task Success | `task_success` |
| Hallucination Resistance / Grounding | `hallucination_resistance` |
| Tool-Use Correctness | `tool_use` |
| Safety & Guardrail Compliance | `safety`, `manipulation_resistance` |
| Response Quality | `instruction_following` |

**Scenarios** ([proofagent/traps/](proofagent/traps/)). Twelve project-specific traps, each a Markdown file with a
pattern, seed utterances, pass criteria and fail criteria. The harness's attacker LLM writes the actual turns from
these, so the wording differs between runs with different seeds.

| Scenario | Covers | Kind |
|---|---|---|
| `sv_constrained_discovery` | product discovery, user constraints, RAG, citations | representative |
| `sv_multi_turn_carryover` | follow-ups, comparison, constraint carry-over | representative |
| `sv_catalog_vs_live_conflict` | MCP tool calls, conflicting private and live information | edge case |
| `sv_budget_pressure` | products outside the budget, social pressure to misstate prices | adversarial |
| `sv_missing_product` | missing / unavailable products | edge case |
| `sv_unsupported_claims` | ratings, certifications, germ-kill and best-seller claims | adversarial |
| `sv_ambiguous_request` | ambiguous requests | edge case |
| `sv_poisoned_tool_result` | incorrect tool results, indirect prompt injection | adversarial |
| `sv_phantom_actions` | claiming actions without a tool (orders, cart, coupons) | adversarial |
| `sv_noisy_voice_transcript` | voice interaction: disfluencies, homophones, spoken self-correction | edge case |
| `sv_unsafe_requests` | chemical mixing, harm | adversarial |
| `sv_secret_extraction` | system prompt and credential extraction | adversarial |

**Running it.**

```bash
python eval/run_proofagent.py                       # full run from config.yaml
python eval/run_proofagent.py --llm anthropic/claude-sonnet-5-5
python eval/run_proofagent.py --voice               # attacker messages through TTS -> ASR
python eval/run_proofagent.py --turns 4 --tag smoke # quick check
proof run eval/proofagent/agent.py --context-dir eval/proofagent/context \
      --domain-knowledge-dir eval/proofagent/knowledge --extra-traps eval/proofagent/traps   # the harness CLI works too
```

Choose a strong harness model for a scorecard you intend to report: the harness documentation notes that a weak
judge gives noisy scores. A local judge (`ollama_chat/qwen3:14b`, `PROOFAGENT_REASONING_EFFORT=none`) works for a
smoke run but is slow (about a minute per jury call on an RTX 5090) and less reliable.

## Retrieval benchmark

[run_retrieval_eval.py](run_retrieval_eval.py) calls `ProductStore.search`, the function behind `rag.search`.
It doesn't use the LLM or the MCP transport, so a run takes about 30 s and gives the same result every time. The
queries and relevance criteria are in [retrieval/queries.yaml](retrieval/queries.yaml).

**Queries (65).**

| Type | n | Relevance | Metrics |
|---|---|---|---|
| Named known item ("Melissa and Doug safari chunky puzzle") | 22 | the product's doc_id, found by title search and checked by hand | Hit@1, Hit@5, MRR@10 |
| Described known item ("purple plush cat with a unicorn horn that makes a sound") | 9 | same; the description is taken from the product's feature text, so it shares few words with the title | Hit@1, Hit@5, MRR@10 |
| Attribute ("wooden puzzle for a toddler", max $20) | 24 | a rule over title, text, category and age: grade 1 = the kind of product asked for, grade 2 = also meets the preferences (e.g. age). Filters are applied as the Planner applies them | P@5 (over min(5, relevant in catalog)), nDCG@10 against the best possible ordering |
| No match ("gas lawn mower for my yard") | 10 | nothing relevant exists; only toys share the words | false match = a `strong` label in the top 5 |

**Why rules, not hand-picked lists.** Relevance never comes from a retriever's output, because then a method would
be graded on its own picks. The criteria were written first. They were then **audited**: `--audit` lists every
product that any mode put in its top 5 and the rules graded 0. Reading that list turned up 20 relevant products the
first criteria had missed. For example, "Battery Powered" RC cars were excluded as parts, and magnetic tiles turned
out to be filed under "Magnetic & Felt Playboards". The criteria were corrected, and every mode was re-scored with
the corrected version. What is still graded 0 in any top 5 is genuinely off-target: pool floats for "water gun",
replacement wheels for "kick scooter".

**Ablation.** `search(mode=...)` turns off one component at a time:
- `dense`: vector similarity only.
- `bm25`: BM25 only.
- `hybrid_rrf`: reciprocal rank fusion of both, without the title rerank.
- `hybrid`: the default, which the agent uses.

Filters and relevance labels are identical in every mode. Besides the means, the report gives **paired** win/loss
counts per attribute query. With 24 queries, a difference in the mean can come from one or two queries, and the
paired counts show whether it does.

**Labels.** The agent only uses `strong` hits as evidence, so the report also scores the label:
- precision and recall of `strong` in the top 5;
- **missed matches**: a relevant product is retrieved, but none of the relevant ones is labelled strong;
- **false matches**: a no-match query gets a strong hit.

**Commands.**

```bash
python eval/run_retrieval_eval.py                       # all modes -> eval/results/retrieval_eval.{md,json}
python eval/run_retrieval_eval.py --show water_gun      # top 10 per mode, with grades, for one query
python eval/run_retrieval_eval.py --audit               # top-5 products the criteria grade 0, for review
```

Results and the two failures traced end to end are in [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md) (section 2b, F11, F12).

## Stage-level checks

ProofAgent reports at the level of the conversation. To attribute a failure to a stage,
[run_stage_checks.py](run_stage_checks.py) runs the 20 scenarios in [stage_scenarios.yaml](stage_scenarios.yaml)
and assigns every expectation to the stage responsible for it:

| Stage | Example checks |
|---|---|
| ASR (voice mode) | word error rate of the transcript against what was said <= 0.15 |
| Router | task label, extracted budget / brand, `wants_live_info`, unsafe flag |
| Planner | planned tools; tools that must not be called |
| RAG/MCP | tool actually called, all evidence within budget, conflict detected, picks are the right product type |
| Answerer / Critic | number of recommendations, every pick cites a retrieved doc_id, required and forbidden strings, no rating claim, critic says grounded |
| TTS | spoken summary <= 45 words; in voice mode audio produced and <= 15 s |

Running it with `LLM_PROVIDER=rules` gives a deterministic baseline with no model; comparing that with an LLM run
separates failures caused by the model from failures caused by retrieval or data.

## Results

See [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md) for the scorecards, the per-stage table, and the analysis of failures.
Raw outputs are in [results/](results/).
