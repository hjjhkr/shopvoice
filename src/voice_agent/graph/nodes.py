"""LangGraph nodes: ASR -> Router -> Planner -> Retriever -> Answerer <-> Critic -> Finalize -> TTS.

Every node appends one entry to state["steps"], which is what the UI's execution
log and logs/agent_runs.jsonl show. Each LLM node has a deterministic fallback, and
hard constraints (budget, citations, tool allow-list) are enforced in code, not by prompt.
"""
from __future__ import annotations

import json
import operator
import os
import re
import time
from typing import Annotated, Any, TypedDict

from ..catalog import CATALOG_NAME, PRICE_SNAPSHOT
from ..config import get_settings
from ..input_guard import log_record, scan_input
from ..llm import call_structured, get_chat_model, llm_enabled, load_prompt, render
from ..logging_utils import log_event
from ..mcp_client import get_mcp_client, llm_tool_name
from ..safety import (CHEMICAL_SAFETY_NOTE, INJECTION_REPLY, REFUSAL, _is_denied, check_input, check_output,
                      sanitize_tool_text, unsupported_claims)
from ..schemas import AnswerDraft, Critique, Intent, Pick, Plan, ToolStep
from .reconcile import reconcile
from .rules import extract_budget, is_target_surface, rule_intent

MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6
MAX_REVISIONS = 1
MAX_SPOKEN_WORDS = 45
MAX_SPOKEN_SECONDS = 15
VERY_STRONG_SIM = 0.60      # similarity needed to accept a hit that does not contain the product's head noun
PRODUCT_TASKS = {"product_search", "product_comparison", "price_availability_check", "product_question"}


class AgentState(TypedDict, total=False):
    # inputs
    audio_in: str
    transcript: str
    history: list[dict]
    synthesize: bool
    # working state
    asr: dict
    input_scan: dict
    input_guard_stop: bool
    intent: dict
    direct_reply: str
    plan: dict
    tool_calls: Annotated[list[dict], operator.add]
    evidence: list[dict]
    near_misses: list[dict]
    live_results: list[dict]
    conflicts: list[dict]
    relaxations: list[str]
    spoken_notes: list[str]
    answer: dict
    critique: dict
    revisions: int
    # outputs
    final_text: str
    spoken_summary: str
    citations: list[dict]
    audio_out: dict
    steps: Annotated[list[dict], operator.add]


def _guard(action: str, mechanism: str, detail: str = "", category: str = "") -> dict:
    """One guardrail event, recorded in the step log so the UI can show what intervened and where.
    action: blocked | diverted | flagged | policy | sanitised | constraint | rejected | repaired"""
    return {"action": action, "mechanism": mechanism, "category": category, "detail": detail}


def _step(node: str, t0: float, summary: str, **detail: Any) -> list[dict]:
    return [{"node": node, "ms": round((time.perf_counter() - t0) * 1000), "summary": summary, "detail": detail}]


def web_available() -> bool:
    from ..mcp_server.web import provider_ready
    return provider_ready(get_settings())


def _money(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


# ============================================================================ ASR
def asr_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    if state.get("transcript") or not state.get("audio_in"):
        return {"steps": _step("asr", t0, "text input (no audio)")}
    from ..speech.asr import transcribe
    try:
        result = transcribe(state["audio_in"])
    except Exception as e:  # noqa: BLE001
        return {"transcript": "", "asr": {"error": str(e)},
                "steps": _step("asr", t0, f"ASR failed: {type(e).__name__}", error=str(e))}
    return {"transcript": result["text"], "asr": result,
            "steps": _step("asr", t0, f"transcribed {result.get('duration')} s of audio with {result['provider']}:{result['model']}",
                           text=result["text"], segments=result["segments"])}


# ================================================================ Input guard (ProofAgent Nano)
GUARD_BLOCK_REPLY = ("I can't help with that request: it was stopped by the input safety check before it reached the assistant. "
                     "I can help you find and compare products from the catalog instead.")
GUARD_REVIEW_REPLY = ("I can't continue with this request here: the input safety check held it for human review, "
                      "so nothing further was run.")
GUARD_ERROR_REPLY = ("The input safety check is unavailable right now, so I stopped before processing your request. "
                     "Please try again in a moment.")
SELF_HARM_NOTE = (" If you are thinking about hurting yourself, please reach out now: in the US call or text 988 "
                  "(Suicide & Crisis Lifeline), or contact your local emergency number.")


def input_guard_node(state: AgentState) -> dict:
    """Scans the transcript (typed or from ASR) with ProofAgent Nano before the Router. On block / review, or a failed
    scan with PROOFAGENT_GUARD_ON_ERROR=block, the graph goes straight to respond_direct: no Router, no LLM, no MCP tool."""
    t0 = time.perf_counter()
    transcript = state.get("transcript") or ""
    g = scan_input(transcript)
    if g.source != "disabled" or transcript.strip():
        log_event("input_guard", log_record(g, transcript))
    label = {"proofagent_nano": "ProofAgent Nano (live API)", "mock": "MOCKED ProofAgent response (test only)",
             "disabled": "ProofAgent scan off", "error": "ProofAgent scan FAILED (no ProofAgent verdict)"}[g.source]
    verdict = f"{g.action} -> {g.decision}" if g.action else g.decision
    summary = f"{label}: {verdict}" + (f" ({g.top_risk})" if g.top_risk else "") + (f"; {g.error}" if g.error else "")
    detail = {"scan": g.to_dict(), "source_label": label}
    detail["scan"].pop("text", None)
    out: dict[str, Any] = {"input_scan": detail["scan"]}

    if g.decision in ("block", "review"):
        if g.source == "error":
            reply, category, mech = GUARD_ERROR_REPLY, "scan_unavailable", "ProofAgent scan failed (fail closed)"
        else:
            reply = GUARD_REVIEW_REPLY if g.decision == "review" else GUARD_BLOCK_REPLY
            if any("self_harm" in r for r in [g.top_risk or "", *g.risks]):
                reply += SELF_HARM_NOTE
            category, mech = g.top_risk or g.action or g.decision, f"{label} · {g.action}"
        flag = "prompt_injection" if (g.top_risk or "").startswith("6.") else "other"
        intent = Intent(task="unsafe", safety_flag=flag, safety_reason=f"input guard: {verdict}")
        out.update(intent=intent.model_dump(), direct_reply=reply, input_guard_stop=True)
        out["steps"] = _step("input_guard", t0, summary, guard=[_guard(
            "blocked", mech, (g.summary or g.note or g.error or "") + " The Router, the LLM and the MCP tools were not run.",
            category)], **detail)
        return out

    guards = []
    if g.masked:
        out["transcript"] = g.text
        guards.append(_guard("sanitised", f"{label} · {g.action}", "Personal data was replaced with placeholders "
                             "before the Router; only the masked text goes on.", g.top_risk or "pii"))
    elif g.source == "error":
        guards.append(_guard("flagged", label, g.note, "scan_unavailable"))
    elif g.note and g.source != "disabled":
        guards.append(_guard("flagged", label, g.note, g.top_risk or g.action or ""))
    out["steps"] = _step("input_guard", t0, summary, guard=guards, **detail)
    return out


def route_after_input_guard(state: AgentState) -> str:
    return "respond_direct" if state.get("input_guard_stop") else "router"


# ========================================================================= Router
def _fmt_history(history: list[dict]) -> str:
    if not history:
        return "(none)"
    lines = []
    for h in history[-8:]:
        lines.append(f"User: {h.get('user', '')[:500]}")
        shown = "; ".join(f"{p['title'][:50]} {_money(p.get('price'))} [{p['doc_id']}]" for p in h.get("shown", []))
        lines.append(f"Assistant: {h.get('assistant', '')}" + (f" (shown: {shown})" if shown else ""))
        c = {k: v for k, v in h.get("intent", {}).get("constraints", {}).items() if v}
        if c:
            lines.append(f"Constraints then: {json.dumps(c)} | search_query then: {h.get('intent', {}).get('search_query', '')}")
    return "\n".join(lines)


def _mentions_amount(text: str, amount: float) -> bool:
    """True if the amount occurs in the text as digits or words ('five', '$5', '5.00', '9.5')."""
    from .rules import spoken_numbers_to_digits
    t = spoken_numbers_to_digits(text.lower())
    whole = int(amount) if float(amount).is_integer() else None
    forms = {f"{amount:g}", f"{amount:.2f}"} | ({str(whole)} if whole is not None else set())
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", t) for f in forms)


OUT_OF_SCOPE_REPLY = ("I can't place, confirm or track orders, manage a cart, apply coupons or reserve stock, and no order has "
                      "been placed through me. I can find, compare and price-check products in the catalog. "
                      "What are you shopping for?")
GENERAL_OUT_OF_SCOPE_REPLY = ("That is outside what I can help with. I find, compare and price-check household supplies "
                              "from the catalog: cleaning, laundry, dishwashing, paper goods and more. What are you shopping for?")
_ORDER_WORDS = re.compile(r"\b(order|orders|cart|checkout|check out|coupon|promo|refund|receipt|reserve|track|deliver|shipping)\b", re.I)


def router_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    transcript = (state.get("transcript") or "").strip()
    history = state.get("history") or []

    if not transcript:
        intent = Intent(task="clarification_needed", clarification_question="Sorry, I didn't catch that. What are you shopping for?")
        return {"intent": intent.model_dump(),
                "steps": _step("router", t0, "empty transcript -> ask again", source="rules",
                               guard=[_guard("diverted", "Router route", "nothing was recognised; asked the user to repeat", "clarification_needed")])}

    verdict = check_input(transcript)          # deterministic guardrail runs before any LLM call
    # Harm and chemical-mixing patterns always block. An instruction-like phrase ("ignore previous
    # instructions") blocks outright only without an LLM: with one, the Router decides whether the user is
    # attacking the assistant or merely quoting a note or a post inside a normal request (blocking those
    # wholesale refused legitimate searches).
    soft = verdict.block and verdict.flag == "prompt_injection" and llm_enabled()
    if verdict.block and not soft:
        intent = Intent(task="unsafe", safety_flag=verdict.flag, safety_reason="blocked by input guardrail")
        return {"intent": intent.model_dump(), "direct_reply": verdict.reply,
                "steps": _step("router", t0, f"guardrail blocked input ({verdict.flag})", source="guardrail", intent=intent.model_dump(),
                               guard=[_guard("blocked", "Input guardrail (pattern match, before any LLM call)",
                                             "The request matched a blocked pattern. No model was called and no tool was run.",
                                             verdict.flag)])}

    rule = rule_intent(transcript, history)
    llm = call_structured("router", Intent, render("router", history=_fmt_history(history), transcript=transcript))
    intent, source, notes = (llm, "llm", []) if llm else (rule, "rules", [])
    if soft and not llm:      # the LLM call failed: fall back to the hard block
        intent = Intent(task="unsafe", safety_flag="prompt_injection", safety_reason="blocked by input guardrail")
        return {"intent": intent.model_dump(), "direct_reply": verdict.reply,
                "steps": _step("router", t0, "guardrail blocked input (prompt_injection)", source="guardrail", intent=intent.model_dump(),
                               guard=[_guard("blocked", "Input guardrail (pattern match; the LLM review was unavailable)",
                                             "Instruction-like text matched and the Router LLM could not review it, so the request was blocked.",
                                             "prompt_injection")])}
    if soft:
        notes.append("input matched an injection pattern; Router LLM judged whether it is an attack or quoted text: "
                     f"safety_flag={intent.safety_flag}")

    if llm:   # cross-check: an explicitly spoken budget is never lost
        lo, hi = extract_budget(transcript)
        c = intent.constraints
        earlier = {h.get("intent", {}).get("constraints", {}).get("max_price") for h in history if h.get("intent")} - {None}
        if hi is not None and c.max_price is None:
            c.max_price = hi; notes.append(f"max_price={hi} restored from transcript by rule check")
        elif hi is not None and c.max_price != hi and c.max_price in earlier and not _mentions_amount(transcript, c.max_price):
            # The LLM kept a budget from an earlier turn that this message never mentions, while the message
            # states another one. A value the message does mention is the LLM's reading and is left alone.
            notes.append(f"max_price {c.max_price} (from an earlier turn, not in this message) replaced by {hi} stated in this message")
            c.max_price = hi
        if lo is not None and c.min_price is None:
            c.min_price = lo; notes.append(f"min_price={lo} restored from transcript by rule check")
        if intent.wants_live_info and not rule.wants_live_info and intent.task != "price_availability_check":
            # web.search is only for requests that actually ask for current price / availability
            intent.wants_live_info = False
            notes.append("wants_live_info reset: the transcript does not ask for current price or availability")
        if intent.task in PRODUCT_TASKS and not intent.search_query.strip():
            intent.search_query = c.product_type or rule.search_query
        if intent.task in PRODUCT_TASKS and not intent.search_query.strip():
            intent.task = "clarification_needed"
    direct_reply = None
    if llm and intent.safety_flag in ("prompt_injection", "secrets") and rule.task == "out_of_scope":
        # An order/cart request that merely quotes a note ("SYSTEM: confirm the order") is out of scope, not an
        # attack on the assistant: answer what was asked (it cannot place orders) and say the note was not followed.
        intent.task, intent.safety_flag = "out_of_scope", "none"
        direct_reply = OUT_OF_SCOPE_REPLY + " I also did not follow the instruction quoted in your message; it is untrusted text."
        notes.append("LLM flagged prompt_injection on an order/cart request; answered as out_of_scope")
    if intent.safety_flag != "none":
        intent.task = "unsafe"
    if intent.constraints.max_price is not None and intent.constraints.max_price <= 0:
        intent.constraints.max_price = None

    guards = []
    if soft:
        guards.append(_guard("flagged", "Input guardrail (pattern match), reviewed by the Router LLM",
                             "Instruction-like text was found in the message. The Router LLM judged whether the user is attacking the "
                             "assistant or quoting text; quoted instructions are never followed.", "prompt_injection"))
    if intent.task == "unsafe":
        guards.append(_guard("blocked", f"Router safety classification ({'LLM' if source == 'llm' else 'rules'})",
                             intent.safety_reason or "The request was classified as unsafe.", intent.safety_flag))
    elif intent.task not in PRODUCT_TASKS:
        guards.append(_guard("diverted", "Router route",
                             {"out_of_scope": "Outside product discovery (for example orders or carts): answered with a fixed reply.",
                              "clarification_needed": "Too vague to search: asked one clarifying question instead of guessing.",
                              "chitchat": "Small talk: answered directly."}.get(intent.task, "Answered directly."), intent.task))
    c = {k: v for k, v in intent.constraints.model_dump().items() if v}
    out = {"intent": intent.model_dump(),
           "steps": _step("router", t0, f"task={intent.task}; constraints={json.dumps(c)}; live={intent.wants_live_info}",
                          source=source, intent=intent.model_dump(), notes=notes, guard=guards)}
    if direct_reply:
        out["direct_reply"] = direct_reply
    return out


def route_after_router(state: AgentState) -> str:
    return "planner" if state["intent"]["task"] in PRODUCT_TASKS else "respond_direct"


def respond_direct_node(state: AgentState) -> dict:
    """Refusals, clarifications and small talk: fixed wording, no tools, no product claims."""
    t0 = time.perf_counter()
    intent = state["intent"]
    task, flag = intent["task"], intent.get("safety_flag", "none")
    if state.get("direct_reply"):
        text = state["direct_reply"]
    elif task == "unsafe":
        text = {"hazardous_chemical": CHEMICAL_SAFETY_NOTE, "prompt_injection": INJECTION_REPLY,
                "secrets": INJECTION_REPLY}.get(flag, REFUSAL)
    elif task == "clarification_needed":
        text = intent.get("clarification_question") or "What kind of product are you looking for, and do you have a budget?"
    elif task == "chitchat":
        text = "Hi! Tell me what you're shopping for and your budget, and I'll find options from the catalog."
    elif _ORDER_WORDS.search(state.get("transcript") or ""):
        text = OUT_OF_SCOPE_REPLY
    else:
        text = GENERAL_OUT_OF_SCOPE_REPLY
    text = check_output(text)
    return {"final_text": text, "spoken_summary": _trim_spoken(text), "citations": [],
            "answer": {"recommendations": [], "caveats": [], "follow_up_question": ""},
            "critique": {"verdict": "approve", "grounded": True, "safe": True, "issues": [],
                         "note": "direct reply with fixed wording; no product claims to verify"},
            "steps": _step("respond_direct", t0, f"direct reply for task={task}", flag=flag)}


# ======================================================================== Planner
def _rule_plan(intent: Intent, web_ok: bool) -> Plan:
    c = intent.constraints
    query = intent.search_query
    for p in c.preferences:
        if p.lower() not in query.lower():
            query += f" {p}"
    steps = [ToolStep(tool="rag.search", query=query.strip(), max_price=c.max_price, min_price=c.min_price,
                      brand=c.brand, category=c.category, min_rating=c.min_rating,
                      purpose="find catalog products that satisfy the constraints")]
    use_web = intent.wants_live_info and web_ok
    if use_web:
        steps.append(ToolStep(tool="web.search", query=f"{c.brand or ''} {intent.search_query} price".strip(),
                              purpose="current price / availability for the top catalog matches"))
    criteria = ["matches the requested product type"]
    if c.max_price is not None or c.min_price is not None:
        criteria.append("fits the budget")
    criteria += [f"matches preference: {p}" for p in c.preferences] + ["lower price"]
    return Plan(use_rag=True, use_web=use_web, steps=steps,
                retrieval_fields=["title", "price", "rating", "brand", "ingredients", "features"],
                comparison_criteria=criteria,
                rationale="Catalog facts come from rag.search" + ("; live price requested so web.search is added." if use_web else "."))


def _validate_plan(plan: Plan, intent: Intent, web_ok: bool) -> tuple[Plan, list[str]]:
    """Code-level policy on top of whatever the LLM planned."""
    notes: list[str] = []
    c = intent.constraints
    steps, seen = [], set()
    for s in plan.steps:
        if s.tool == "web.search" and not (intent.wants_live_info and web_ok):
            notes.append("dropped web.search step: " + ("live search is not configured" if not web_ok else "user did not ask for live info"))
            continue
        if s.tool == "rag.search":   # hard constraints always travel with the query
            s.max_price = c.max_price if c.max_price is not None else s.max_price
            s.min_price = c.min_price if c.min_price is not None else s.min_price
            s.brand = c.brand or s.brand
            s.min_rating = c.min_rating if c.min_rating is not None else s.min_rating
        key = (s.tool, s.query.lower().strip())
        if key in seen or not s.query.strip():
            continue
        seen.add(key)
        steps.append(s)
    if not any(s.tool == "rag.search" for s in steps):
        steps = _rule_plan(intent, False).steps + steps
        notes.append("added rag.search: every product answer must be grounded in the catalog")
    if intent.wants_live_info and web_ok and not any(s.tool == "web.search" for s in steps):
        steps.append(ToolStep(tool="web.search", query=f"{c.brand or ''} {intent.search_query} price".strip(),
                              purpose="user asked for current price / availability"))
        notes.append("added web.search: user asked for live info")
    if intent.wants_live_info and not web_ok:
        notes.append("user asked for live info but web.search is not configured; answering from the catalog only")
    plan.steps = steps[:4]
    plan.use_rag = True
    plan.use_web = any(s.tool == "web.search" for s in plan.steps)
    return plan, notes


def planner_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    intent = Intent(**state["intent"])
    web_ok = web_available()
    tools = get_mcp_client().list_tools()          # MCP discovery
    tools_txt = "\n".join(f"- {t['name']}: {t['description']}\n  input schema: {json.dumps(t['input_schema'].get('properties', {}))}"
                          for t in tools)
    llm = call_structured("planner", Plan, render("planner", tools=tools_txt, intent=json.dumps(state["intent"]),
                                                  web_available="yes" if web_ok else "no (not configured)"))
    plan, notes = _validate_plan(llm or _rule_plan(intent, web_ok), intent, web_ok)
    out = plan.model_dump()
    out["notes"] = notes
    summary = " -> ".join(f"{s.tool}({s.query!r}" + (f", max_price={s.max_price}" if s.max_price is not None else "") + ")"
                          for s in plan.steps)
    return {"plan": out, "steps": _step("planner", t0, summary, source="llm" if llm else "rules", plan=out,
                                        discovered_tools=[t["name"] for t in tools],
                                        guard=[_guard("policy", "Planner policy check (code)", note) for note in notes])}


# ====================================================================== Retriever
_RAG_ARGS = {"query", "top_k", "max_price", "min_price", "brand", "category", "min_rating"}


def _enforce_args(tool: str, args: dict, constraints: dict) -> dict:
    """Hard constraints are applied to every catalog call regardless of what the LLM passed."""
    if tool == "web.search":
        return {"query": str(args.get("query", ""))[:200], "max_results": min(int(args.get("max_results") or 5), 8)}
    clean = {k: v for k, v in args.items() if k in _RAG_ARGS and v not in (None, "")}
    clean["query"] = str(clean.get("query", ""))[:200]
    clean["top_k"] = max(5, min(int(clean.get("top_k") or 6), 10))
    for key in ("max_price", "min_price", "min_rating"):
        if constraints.get(key) is not None:
            clean[key] = constraints[key]
    if constraints.get("brand"):
        clean["brand"] = constraints["brand"]
    if constraints.get("category"):
        clean["category"] = constraints["category"]
    return clean


def _brief(tool: str, results: list[dict]) -> list[dict]:
    """Compact, loggable view of what a tool returned (shown to the Answerer, the Critic and the eval jury)."""
    if tool == "rag.search":
        return [{"doc_id": r.get("doc_id"), "title": (r.get("title") or "")[:70], "price": r.get("price")} for r in results[:6]]
    return [{"title": (r.get("title") or "")[:70], "url": r.get("url"), "price": r.get("price"),
             "availability": r.get("availability")} for r in results[:5] if not r.get("suspicious")]


def _tool_calls_text(calls: list[dict]) -> str:
    if not calls:
        return "(none)"
    lines = []
    for c in calls:
        args = ", ".join(f"{k}={v!r}" for k, v in c["args"].items() if k != "top_k")
        lines.append(f"- {c['tool']}({args}) -> {c['count']} result(s)" + (f", error: {c['error']}" if c.get("error") else ""))
    return "\n".join(lines)


def _head_noun(product_type: str | None) -> str | None:
    """'stainless steel cleaner for kitchens' -> 'cleaner'; 'Swiffer refill packs' -> None (generic). The thing itself, used to
    reject hits that share only an adjective with the request."""
    if not product_type:
        return None
    core = re.split(r"\b(?:for|with|under|over|from|by|in|that|to|at|of)\b", product_type.lower())[0]
    words = re.findall(r"[a-z]+", core)
    if not words:
        return None
    head = words[-1]
    if len(head) > 3 and head.endswith("s") and not head.endswith("ss"):
        head = head[:-1]
    if head in _GENERIC_HEADS:      # "a Clorox pack", "a gift": the word names no product type to verify
        return None
    return head if len(head) > 2 else None


_GENERIC_HEADS = {"set", "kit", "toy", "gift", "present", "option", "item", "product", "thing", "stuff", "one", "pack",
                  "piece", "something", "anything", "choice", "pick", "supply", "supplie", "refill", "bundle", "brand",
                  "count", "bottle", "box", "case", "variety"}


NAME_MATCH = 0.6      # share of a named product's words that must occur in a catalog title


def _name_coverage(name: str, title: str) -> float:
    words = {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 1 and w not in {"the", "and", "for", "with", "price", "of"}}
    if len(words) < 2:
        return 0.0          # a single word ("detergent") is a product type, not a named product
    return len(words & set(re.findall(r"[a-z0-9]+", title.lower()))) / len(words)


def _live_targets(products: list[dict], said: set[str], limit: int = 2) -> list[dict]:
    """The products that get a live price lookup, the ones the user named first.

    A product is scored by the spoken words in its title, ignoring words that say nothing about which product
    is meant: stop words and words found in most candidate titles ("laundry", "detergent", the brand). When the
    user named a product, candidates with less than half the best score are skipped, so "the Tru Earth detergent
    strips" does not also spend a search on another brand's detergent. With no named product the top `limit` matches are checked."""
    titles = [set(re.findall(r"[a-z0-9]+", p["title"].lower())) for p in products]
    common = {t for t in set().union(*titles) if sum(t in x for x in titles) * 2 > len(titles)} if len(titles) > 1 else set()
    skip = common | {"the", "and", "for", "with", "of", "in", "is", "it", "to", "a", "s"}
    score = {p["doc_id"]: len((t & said) - skip) for p, t in zip(products, titles)}
    ranked = sorted(products, key=lambda p: score[p["doc_id"]], reverse=True)
    best = score[ranked[0]["doc_id"]] if ranked else 0
    return [p for p in ranked[:limit] if score[p["doc_id"]] * 2 >= best] if best else ranked[:limit]


def _compact(tool: str, out: dict) -> str:
    """What the Retriever LLM sees of a tool result (short, and already sanitised)."""
    if tool == "rag.search":
        rows = [{k: r.get(k) for k in ("doc_id", "title", "price", "brand", "ingredients", "relevance")} for r in out.get("results", [])]
    else:
        rows = [{k: r.get(k) for k in ("title", "url", "price", "availability")} for r in out.get("results", [])]
    return json.dumps({"results": rows, "error": out.get("error")})[:3500]


def retriever_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    client = get_mcp_client()
    intent, plan = state["intent"], state["plan"]
    user_constraints = intent["constraints"]
    constraints = user_constraints
    price_check = intent["task"] == "price_availability_check"
    web_ok = web_available()

    calls: list[dict] = []
    rag: dict[str, dict] = {}
    web: list[dict] = []
    executed: set[str] = set()
    relaxations: list[str] = []
    spoken_notes: list[str] = []      # short versions of the relaxations, for the spoken summary
    denied: list[str] = []            # tool calls the model asked for that policy refused

    def run(tool: str, args: dict, origin: str) -> dict:
        if tool == "web.search" and not (plan.get("use_web") and web_ok):
            denied.append("web.search")
            return {"results": [], "error": "web.search is not part of the approved plan"}
        args = _enforce_args(tool, args, constraints)
        key = tool + json.dumps(args, sort_keys=True)
        if key in executed or len(calls) >= MAX_TOOL_CALLS or not args["query"].strip():
            return {"results": [], "error": "duplicate or over the tool-call limit"}
        executed.add(key)
        t = time.perf_counter()
        out = client.call_tool(tool, args)
        results = out.get("results", []) or []
        flagged = 0
        for r in results:      # tool output is data: neutralise instruction-like text before any LLM sees it
            for field in ("title", "features", "snippet"):
                if r.get(field):
                    r[field], sus = sanitize_tool_text(r[field])
                    flagged += sus
        if tool == "rag.search":
            for r in results:
                rag.setdefault(r["doc_id"], r)
        else:
            web.extend(results)
        calls.append({"tool": tool, "args": args, "origin": origin, "count": len(results), "error": out.get("error"),
                      "ms": round((time.perf_counter() - t) * 1000), "injection_flagged": flagged,
                      "ids": [r.get("doc_id") or r.get("url") for r in results], "results": _brief(tool, results)})
        return out

    # --- 1. agentic tool loop: the LLM issues MCP tool calls, guided by the plan
    mode = "plan-executor"
    model = get_chat_model()
    if model is not None:
        try:
            from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
            tools = client.list_tools()
            names = {llm_tool_name(t["name"]): t["name"] for t in tools if t["name"] != "web.search" or (plan.get("use_web") and web_ok)}
            specs = [{"type": "function", "function": {"name": llm_tool_name(t["name"]), "description": t["description"],
                                                       "parameters": t["input_schema"]}}
                     for t in tools if llm_tool_name(t["name"]) in names]
            bound = model.bind_tools(specs)
            # Show the plan with the function names the model can actually call (rag_search, not rag.search):
            # models copy the name verbatim, and a dotted name is not a valid function name for LLM APIs.
            plan_for_llm = {"steps": [{**{k: v for k, v in s.items() if v not in (None, "")}, "tool": llm_tool_name(s["tool"])}
                                      for s in plan["steps"] if llm_tool_name(s["tool"]) in names],
                            "comparison_criteria": plan.get("comparison_criteria")}
            prompt = render("retriever", max_rounds=MAX_TOOL_ROUNDS, intent=json.dumps(intent), plan=json.dumps(plan_for_llm))
            messages: list = [SystemMessage(load_prompt("system")), HumanMessage(prompt)]
            log_event("prompts", {"node": "retriever", "system": load_prompt("system"), "user": prompt, "tools": list(names.values())})
            s = get_settings()
            for round_no in range(MAX_TOOL_ROUNDS):
                t_llm = time.perf_counter()
                ai = bound.invoke(messages)
                messages.append(ai)
                log_event("prompts", {"node": "retriever", "provider": s.llm_provider, "model": s.llm_model, "round": round_no,
                                      "output": [{"tool": tc["name"], "args": tc.get("args")} for tc in getattr(ai, "tool_calls", None) or []],
                                      "usage": getattr(ai, "usage_metadata", None),
                                      "latency_ms": round((time.perf_counter() - t_llm) * 1000)})
                if not getattr(ai, "tool_calls", None):
                    break
                mode = "llm-tool-calling"
                for tc in ai.tool_calls:
                    tool = names.get(tc["name"])
                    out = run(tool, dict(tc.get("args") or {}), "llm") if tool else {"results": [], "error": f"unknown tool {tc['name']}"}
                    messages.append(ToolMessage(content=_compact(tool or "", out), tool_call_id=tc["id"]))
        except Exception as e:  # noqa: BLE001 - fall through to the deterministic executor
            log_event("prompts", {"node": "retriever", "error": f"{type(e).__name__}: {e}"})

    # --- 2. safety net: every planned catalog step runs even if the LLM skipped it
    if not any(c["tool"] == "rag.search" for c in calls):
        for s in plan["steps"]:
            if s["tool"] == "rag.search":
                run("rag.search", {k: s.get(k) for k in _RAG_ARGS}, "plan")

    def strong() -> list[dict]:
        return [r for r in rag.values() if r.get("relevance") == "strong"]

    # --- 3. self-correction when nothing usable came back
    if not strong() and constraints.get("min_rating") is not None:
        relaxed = dict(constraints, min_rating=None)
        args = _enforce_args("rag.search", {"query": intent["search_query"]}, relaxed)
        args.pop("min_rating", None)
        key = "rag.search" + json.dumps(args, sort_keys=True)
        if key not in executed:
            executed.add(key)
            out = client.call_tool("rag.search", args)
            for r in out.get("results", []) or []:
                rag.setdefault(r["doc_id"], r)
            calls.append({"tool": "rag.search", "args": args, "origin": "relaxation", "count": len(out.get("results", [])),
                          "error": out.get("error"), "ms": None, "injection_flagged": 0,
                          "ids": [r["doc_id"] for r in out.get("results", [])]})
            relaxations.append(f"No match was rated {constraints['min_rating']:g} stars or higher, so the rating filter was dropped; "
                               "the closest matches are shown with their actual ratings.")
    if not strong():
        simple = constraints.get("product_type") or intent["search_query"]
        if simple and simple.lower() != intent["search_query"].lower():
            run("rag.search", {"query": simple}, "retry-simpler-query")

    other_brand: list[dict] = []
    if not strong() and constraints.get("brand"):
        # Brand filter matched nothing: look at what else exists under the same name, and say so.
        name = constraints.get("product_type") or intent["search_query"]
        args = _enforce_args("rag.search", {"query": f"{constraints['brand']} {name}"}, dict(constraints, brand=None))
        key = "rag.search" + json.dumps(args, sort_keys=True)
        if key not in executed and len(calls) < MAX_TOOL_CALLS:
            executed.add(key)
            out = client.call_tool("rag.search", args)
            found = out.get("results", []) or []
            # Brand is a hard constraint: other brands are never promoted to recommendations. They are only
            # listed as "closest items (not matches)" so the user can see what exists.
            other_brand = [r for r in found if r.get("relevance") == "strong"][:3]
            calls.append({"tool": "rag.search", "args": args, "origin": "relaxation", "count": len(found),
                          "error": out.get("error"), "ms": None, "injection_flagged": 0,
                          "ids": [r["doc_id"] for r in found], "results": _brief("rag.search", found)})
            relaxations.append(f"No {constraints['brand']} product in the catalog matches the request within the stated "
                               "constraints. Products of other brands are not recommended in its place.")

    evidence_rag = strong()

    # --- 3b. verify the product type and the material against the retrieved text (not just similarity)
    head = _head_noun(constraints.get("product_type") or intent["search_query"])
    if head and evidence_rag:
        same_type = [r for r in evidence_rag if re.search(rf"\b{re.escape(head)}(s|es)?\b", f"{r['title']} {r.get('category') or ''}", re.I)]
        if same_type:      # e.g. drop a "glass cleaner wipe" returned for "glass cleaner spray"
            evidence_rag = same_type
        else:
            # Nothing is named like the request. Keep only near-certain semantic matches (synonyms such as
            # "sneakers" for "shoes") and say they are not exact; otherwise this is a no-match.
            evidence_rag = [r for r in evidence_rag if (r.get("similarity") or 0) >= VERY_STRONG_SIM]
            if evidence_rag:
                relaxations.append(f"No catalog product is named '{head}'; the options shown are the closest by description, not exact matches.")
                spoken_notes.append("These are close matches, not exact ones.")
    material = (constraints.get("material") or "").lower().strip()
    if material and is_target_surface(material, f"{constraints.get('product_type') or ''} {state.get('transcript', '')}"):
        material = ""      # "stainless steel cleaner": the surface it cleans, not what it is made of
    if material and evidence_rag:
        stem = re.sub(r"(en|s)$", "", material) if len(material) > 4 else material      # wooden -> wood
        made_of = [r for r in evidence_rag
                   if stem in f"{r['title']} {r.get('ingredients') or ''} {r.get('features') or ''}".lower()]
        if made_of:
            evidence_rag = made_of
        else:
            relaxations.append(f"None of the catalog matches is listed as {material}; the options shown do NOT meet "
                               f"the '{material}' requirement.")
            spoken_notes.append(f"None of them is listed as {material}.")
    evidence_rag = evidence_rag[:6]

    # --- 3c. products the user names are looked up on their own, WITHOUT the budget filter, and shown next to
    # the in-budget matches. "Find a dish soap under $5 and check the Dawn Platinum price" then gets the same
    # answer whichever task label the Router chose, and an over-budget product is reported as over budget
    # instead of "not in the catalog".
    names = [n.strip() for n in (intent.get("named_products") or []) if n and n.strip()][:2]
    if price_check and not names:
        names = [intent["search_query"]]
    for name in names:
        if any(_name_coverage(name, r["title"]) >= NAME_MATCH for r in evidence_rag):
            continue
        args = {"query": name[:200], "top_k": 5}
        if user_constraints.get("brand"):
            args["brand"] = user_constraints["brand"]
        key = "rag.search" + json.dumps(args, sort_keys=True)
        if key in executed or len(calls) >= MAX_TOOL_CALLS:
            continue
        executed.add(key)
        out = client.call_tool("rag.search", args)
        found = out.get("results", []) or []
        calls.append({"tool": "rag.search", "args": args, "origin": "named-product", "count": len(found),
                      "error": out.get("error"), "ms": None, "injection_flagged": 0,
                      "ids": [r["doc_id"] for r in found], "results": _brief("rag.search", found)})
        hit = next((r for r in found if _name_coverage(name, r["title"]) >= NAME_MATCH), None)
        if hit and all(hit["doc_id"] != r["doc_id"] for r in evidence_rag):
            evidence_rag.append(hit)
    for r in evidence_rag:
        r["named"] = any(_name_coverage(n, r["title"]) >= NAME_MATCH for n in names)
    evidence_rag.sort(key=lambda r: not r["named"])
    near_misses = [] if evidence_rag else (other_brand or [r for r in rag.values()])[:3]

    # --- 4. live comparison for the top catalog matches (only when planned)
    if plan.get("use_web") and web_ok and evidence_rag:
        # The products the user actually named come first ("today's Easy-Off price" -> the Easy-Off cleaner), and a
        # live lookup is guaranteed for them even if the LLM searched for something else.
        said = set(re.findall(r"[a-z0-9]+", state.get("transcript", "").lower()))
        targets = _live_targets(evidence_rag, said)
        for p in targets:
            if reconcile([p], web)[0][0].get("live"):
                continue                                     # already has a matching live result
            run("web.search", {"query": f"{p['title'][:80]} price"}, "plan")

    evidence, conflicts, unmatched = reconcile(evidence_rag, web)
    unmatched = [w for w in unmatched if not w.get("suspicious")]     # never surface injected results as sources
    max_price = user_constraints.get("max_price")
    for e in evidence:
        live_price = (e.get("live") or {}).get("price")
        if max_price is not None and live_price is not None and live_price > max_price:
            e["budget_note"] = f"Live price {_money(live_price)} is above the {_money(max_price)} budget (catalog price {_money(e['price'])})."
        elif max_price is not None and e.get("price") is not None and e["price"] > max_price:
            e["budget_note"] = f"Catalog price {_money(e['price'])} is above the {_money(max_price)} budget."

    errors = [f"{c['tool']}: {c['error']}" for c in calls if c.get("error")]
    guards = []
    flagged_fields = sum(c.get("injection_flagged") or 0 for c in calls)
    if flagged_fields:
        guards.append(_guard("sanitised", "Tool-output sanitiser (code)",
                             f"{flagged_fields} field(s) returned by a tool contained instruction-like text. It was removed, and the "
                             "result is excluded from matching and citations.", "prompt_injection"))
    if denied:
        guards.append(_guard("blocked", "Tool policy (approved plan, code)",
                             f"The model asked for {len(denied)} web.search call(s) that the approved plan does not allow; they were not run."))
    guards += [_guard("constraint", "Constraint verification (code)", note) for note in relaxations]
    guards += [_guard("constraint", "Budget check on live price (code)", e["budget_note"]) for e in evidence if e.get("budget_note")]
    summary = (f"{len(calls)} tool call(s) via {mode}: {len(evidence)} grounded product(s), {len(web)} live result(s), "
               f"{len(conflicts)} conflict(s)" + (f", {len(near_misses)} near-miss(es) only" if near_misses else ""))
    return {"tool_calls": calls, "evidence": evidence, "near_misses": near_misses, "live_results": unmatched,
            "conflicts": conflicts, "relaxations": relaxations, "spoken_notes": spoken_notes,
            "steps": _step("retriever", t0, summary, mode=mode, calls=calls, conflicts=conflicts, errors=errors,
                           relaxations=relaxations, guard=guards)}


# ======================================================================= Answerer
def _evidence_for_llm(evidence: list[dict]) -> str:
    rows = []
    for e in evidence:
        row = {"doc_id": e["doc_id"], "title": e["title"], "price": e["price"], "rating": e.get("rating"),
               "rating_count": e.get("rating_count"),
               "brand": (f"{e['brand']} (inferred from title)" if e.get("brand") and e.get("brand_inferred") else e.get("brand")),
               "ingredients_or_materials": e.get("ingredients"), "category": e.get("category"),
               "listing_claims": e.get("claims"), "scent": e.get("scent"), "form": e.get("form"), "size": e.get("size"),
               "features": (e.get("features") or "")[:350], "price_per_oz": e.get("price_per_oz"),
               "price_per_count": e.get("price_per_count"), "recommended_age": e.get("recommended_age")}
        if e.get("price_max"):
            row["price_range_max"] = e["price_max"]
        if e.get("live"):
            row["live"] = {k: e["live"].get(k) for k in ("price", "availability", "url")}
            offers = [o for o in e["live"].get("offers") or [] if o.get("price") is not None]
            if len(offers) > 1:      # several shops: the lowest in-stock price is "the live price", the rest are context
                row["live"]["other_offers"] = [{"price": o["price"], "shop": o["domain"]} for o in offers if o["url"] != e["live"].get("url")][:4]
        if e.get("budget_note"):
            row["budget_note"] = e["budget_note"]
        if e.get("named"):
            row["named_by_user"] = True
        rows.append({k: v for k, v in row.items() if v is not None})
    return json.dumps(rows, indent=1)


def _no_match_evidence(state: AgentState) -> str:
    """What the Answerer and the Critic see when nothing passed the checks: an empty evidence list plus the
    near-misses that the system itself lists on screen as "closest items (not matches)"."""
    near = "; ".join(f"{n['title'][:80]} [{n['doc_id']}] ({_money(n.get('price'))})" for n in (state.get("near_misses") or [])[:3])
    return ("[]  (no catalog product matched this request: there are no picks, and the answer must say so)" +
            (f"\nClosest catalog items. The system lists them on screen as non-matches, so naming them as non-matches "
             f"is grounded; recommending them is not: {near}" if near else ""))


def _live_unmatched_text(state: AgentState) -> str:
    """Live results that matched no catalog product. The Answerer and the Critic get the same list, so a
    truthful "a Wikipedia page came back without a price" is not judged as invented."""
    rows = [{k: w.get(k) for k in ("title", "url", "price", "availability")} for w in (state.get("live_results") or [])[:4]]
    return json.dumps(rows) if rows else "(none)"


def _evidence_text(evidence: list[dict]) -> str:
    return " ".join(f"{e['title']} {e.get('features') or ''} {e.get('ingredients') or ''} {e.get('category') or ''} "
                    f"{e.get('recommended_age') or ''} {e.get('claims') or ''} {e.get('scent') or ''} {e.get('form') or ''} "
                    f"{(e.get('live') or {}).get('title') or ''}" for e in evidence)


def _short_title(title: str, words: int = 7) -> str:
    cut = re.split(r"\s[-–|(,]\s?|,\s", title)[0]
    return " ".join(cut.split()[:words]).rstrip(" -,:&")


def _first_feature(e: dict) -> str:
    feat = re.split(r"\s\|\s|(?<=[.!])\s", (e.get("features") or "").strip())[0].strip()
    return feat[:150].rstrip(" .,") if feat else ""


def _rule_answer(state: AgentState) -> AnswerDraft:
    """Template answer built only from evidence fields (also the repair path after a failed revision)."""
    evidence = state.get("evidence") or []
    c = state["intent"]["constraints"]
    ok = [e for e in evidence if e.get("named") or not e.get("budget_note")] or evidence
    ok = sorted(ok, key=lambda e: not e.get("named"))          # the product the user asked about comes first
    picks = []
    for e in ok[:3]:
        bits = [f"Listed at {_money(e['price'])} in the catalog"]
        if e.get("ingredients"):
            bits.append(f"materials: {e['ingredients']}")
        if e.get("rating") is not None:
            bits.append(f"rated {e['rating']:.1f} out of 5 by {e.get('rating_count') or 'some'} buyers")
        if e.get("recommended_age"):
            bits.append(f"recommended age: {e['recommended_age']}")
        why = "; ".join(bits) + "."
        if _first_feature(e):
            why += f" Catalog description: \"{_first_feature(e)}\"."
        picks.append(Pick(doc_id=e["doc_id"], why=why))
    top = ok[0]
    budget = f" under {_money(c['max_price'])}" if c.get("max_price") is not None else ""
    n = len(picks)
    if top.get("budget_note"):      # a price lookup of a product that turns out to be over budget
        live = (top.get("live") or {}).get("price")
        spoken = (f"{_short_title(top['title'])} is {_money(top['price'])} in the {PRICE_SNAPSHOT} catalog"
                  + (f" and {_money(live)} live" if live is not None else "")
                  + f", which is above your {_money(c['max_price'])} budget. "
                  + (f"{n - 1} option{'s' if n - 1 != 1 else ''} within budget {'are' if n - 1 != 1 else 'is'} on screen."
                     if n > 1 else "Details and sources are on screen."))
    elif (top.get("live") or {}).get("price") is not None and (top.get("named") or state["intent"]["task"] == "price_availability_check"):
        live = top["live"]                # a price lookup: say both prices, the live one with its shop
        shop = re.sub(r"^https?://(www\.)?|/.*$", "", live.get("url") or "")
        stock = {"in_stock": ", in stock", "out_of_stock": ", but out of stock"}.get(live.get("availability"), "")
        spoken = (f"{_short_title(top['title'])} is {_money(top['price'])} in the {PRICE_SNAPSHOT} catalog. The lowest live price I found is "
                  f"{_money(live['price'])}" + (f" at {shop}" if shop else "") + f"{stock}. Sources are on screen.")
    else:
        spoken = (f"I found {n} option{'s' if n != 1 else ''}{budget} in the catalog. My top pick is {_short_title(top['title'])} "
                  f"at {_money(top['price'])}. " + ("The others are on screen with sources. " if n > 1 else "Details and sources are on screen. "))
    return AnswerDraft(picks=picks, spoken_summary=spoken.strip(), caveats=[],
                       follow_up_question="Would you like the most affordable option, or more detail on one of them?")


def _no_match_answer(state: AgentState) -> dict:
    intent = state["intent"]
    c = intent["constraints"]
    what = c.get("product_type") or intent.get("search_query") or "that"
    limits = []
    if c.get("max_price") is not None:
        limits.append(f"under {_money(c['max_price'])}")
    if c.get("min_price") is not None:
        limits.append(f"over {_money(c['min_price'])}")
    if c.get("brand"):
        limits.append(f"from {c['brand']}")
    spoken = (f"I couldn't find a match for {what}" + (" " + " ".join(limits) if limits else "") +
              " in the catalog, so I won't guess. Want me to widen the budget or try a different product?")
    caveats = ["No catalog product matched the request closely enough to recommend."]
    near = state.get("near_misses") or []
    if near:
        caveats.append("Closest catalog items (not matches): " + "; ".join(f"{_short_title(n['title'], 9)} [{n['doc_id']}]" for n in near[:3]))
    return {"recommendations": [], "spoken_summary": spoken, "caveats": caveats,
            "follow_up_question": "Should I widen the budget or try a different product?", "source": "no-match template",
            "dropped_picks": []}


def _hydrate(draft: AnswerDraft, evidence: list[dict]) -> dict:
    """Copy product facts from evidence by doc_id: the LLM never writes prices, ratings or titles."""
    by_id = {e["doc_id"]: e for e in evidence}
    recs, dropped, seen = [], [], set()
    for pick in draft.picks[:3]:
        doc_id = pick.doc_id.strip().strip("[]")
        e = by_id.get(doc_id)
        if e is None or doc_id in seen:
            dropped.append(pick.doc_id)
            continue
        seen.add(doc_id)
        live = e.get("live") or {}
        recs.append({
            "doc_id": doc_id, "sku": e.get("sku"), "title": e["title"], "price": e.get("price"), "price_max": e.get("price_max"),
            "rating": e.get("rating"), "rating_count": e.get("rating_count"), "brand": e.get("brand"),
            "brand_inferred": e.get("brand_inferred"), "ingredients": e.get("ingredients"), "claims": e.get("claims"),
            "scent": e.get("scent"), "size": e.get("size"), "price_per_oz": e.get("price_per_oz"),
            "price_per_count": e.get("price_per_count"), "catalog_url": e.get("url"),
            "recommended_age": e.get("recommended_age"), "brand_source": e.get("brand_source"),
            "why": pick.why.strip(), "live_price": live.get("price"), "live_url": live.get("url"),
            "live_availability": live.get("availability"), "budget_note": e.get("budget_note"),
            "citations": [doc_id] + ([live["url"]] if live.get("url") else []),
        })
    return {"recommendations": recs, "spoken_summary": draft.spoken_summary.strip(), "caveats": list(draft.caveats),
            "follow_up_question": draft.follow_up_question.strip(), "dropped_picks": dropped}


def answerer_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    evidence = state.get("evidence") or []
    revisions = state.get("revisions", 0)

    feedback = ""
    prior = state.get("critique") or {}
    if revisions and prior.get("issues"):
        feedback = ("Your previous draft was rejected by the Critic. Fix every issue below: delete each quoted phrase "
                    "(do not rephrase it), and if a user preference is not stated in a product's evidence, say it "
                    "could not be verified instead of claiming it.\n" +
                    "\n".join(f"- {i}" for i in prior["issues"]) + "\n\n")

    def ask_llm(evidence_text: str) -> AnswerDraft | None:
        return call_structured("answerer", AnswerDraft, render(
            "answerer", feedback=feedback, transcript=state["transcript"], intent=json.dumps(state["intent"]),
            history=_fmt_history(state.get("history") or []), tool_calls=_tool_calls_text(state.get("tool_calls") or []),
            criteria="; ".join(state["plan"].get("comparison_criteria") or []) or "best match first",
            conflicts=(json.dumps(state.get("conflicts") or []) if state.get("conflicts") else "(none)") +
                      ("\n\nRetrieval notes you must mention in the summary and caveats:\n" +
                       "\n".join(f"- {n}" for n in state["relaxations"]) if state.get("relaxations") else ""),
            live_unmatched=_live_unmatched_text(state), evidence=evidence_text))

    if not evidence:
        # Nothing to recommend. The wording still comes from the LLM when one is available, because the message may
        # contain a false premise or a quoted instruction that has to be answered; no product can be picked.
        answer = _no_match_answer(state)
        draft = ask_llm(_no_match_evidence(state))
        if draft:
            answer.update(spoken_summary=draft.spoken_summary.strip() or answer["spoken_summary"],
                          caveats=answer["caveats"] + [c for c in draft.caveats if c not in answer["caveats"]],
                          follow_up_question=draft.follow_up_question.strip() or answer["follow_up_question"],
                          dropped_picks=[p.doc_id for p in draft.picks], source="llm")
        return {"answer": answer, "steps": _step("answerer", t0, f"no grounded evidence -> no-match answer [{answer['source']}]",
                                                 source=answer["source"], spoken=answer["spoken_summary"])}

    draft = ask_llm(_evidence_for_llm(evidence))
    source = "llm" if draft else "template"
    answer = _hydrate(draft or _rule_answer(state), evidence)
    answer["source"] = source
    return {"answer": answer,
            "steps": _step("answerer", t0, f"draft {'revision ' + str(revisions) if revisions else ''} with {len(answer['recommendations'])} pick(s) [{source}]".replace("  ", " "),
                           source=source, picks=[r["doc_id"] for r in answer["recommendations"]], spoken=answer["spoken_summary"])}


# ========================================================================= Critic
_PRICE_IN_TEXT = re.compile(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{1,2})?)")
# A star rating stated in the answer ("4.6 stars", "rated 4.6", "4.6 out of 5") must be one of the evidence ratings.
_RATING_VALUE = re.compile(r"\b(\d(?:\.\d{1,2})?)\s*(?:-|\s)?(?:stars?\b|out of (?:5|five)\b)|\brated\s+(\d(?:\.\d{1,2})?)\b", re.I)
# The catalog has average ratings and rating counts, but no review text: what reviewers "say" cannot be grounded.
_REVIEW_CLAIM = re.compile(r"\breviews? (say|says|praise|mention|report|complain|note)|\breviewers?\b|\b(customers|buyers|users) "
                           r"(say|love|praise|report|complain|mention|note)", re.I)


def _history_prices(history: list[dict] | None) -> set[float]:
    """Budgets and shown prices from earlier turns: legitimate numbers to mention ("replacing the earlier $20 cap")."""
    out: set[float] = set()
    for h in history or []:
        c = h.get("intent", {}).get("constraints", {})
        out.update(v for v in (c.get("max_price"), c.get("min_price")) if v is not None)
        out.update(p["price"] for p in h.get("shown", []) if p.get("price") is not None)
    return out


def deterministic_checks(answer: dict, evidence: list[dict], constraints: dict, conflicts: list[dict],
                         known_prices: set[float] | None = None, price_check: bool = False) -> tuple[list[str], list[str]]:
    """-> (blocking issues, minor issues). These do not depend on any model."""
    blocking, minor = [], []
    by_id = {e["doc_id"]: e for e in evidence}
    recs = answer.get("recommendations") or []
    text = " ".join([answer.get("spoken_summary", "")] + [r["why"] for r in recs] + (answer.get("caveats") or []))

    for d in answer.get("dropped_picks") or []:
        blocking.append(f"Pick '{d}' cites a doc_id that is not in the retrieved evidence (removed).")
    for r in recs:
        if r["doc_id"] not in by_id:
            blocking.append(f"{r['doc_id']} is not in the evidence.")
        if by_id.get(r["doc_id"], {}).get("named"):
            continue        # a product the user asked about by name may be shown over budget; it is labelled as such
        if constraints.get("max_price") is not None and r.get("price") is not None and r["price"] > constraints["max_price"]:
            blocking.append(f"{r['doc_id']} costs {_money(r['price'])}, above the {_money(constraints['max_price'])} budget.")
        if constraints.get("min_price") is not None and r.get("price") is not None and r["price"] < constraints["min_price"]:
            blocking.append(f"{r['doc_id']} costs {_money(r['price'])}, below the {_money(constraints['min_price'])} minimum.")

    allowed = {v for k in ("max_price", "min_price") if (v := constraints.get(k)) is not None} | (known_prices or set())
    for e in evidence:
        allowed.update(v for v in (e.get("price"), e.get("price_max"), e.get("price_per_oz"), e.get("price_per_count"),
                                   (e.get("live") or {}).get("price")) if v is not None)
        allowed.update(o["price"] for o in (e.get("live") or {}).get("offers") or [] if o.get("price") is not None)   # other shops
    for m in _PRICE_IN_TEXT.finditer(text):
        value = float(m.group(1).replace(",", ""))
        tolerance = 0.011 if "." in m.group(1) else 0.5      # "$20" may be a rounded $19.99
        if _is_denied(text, m.start(), m.end()):
            continue      # "I cannot confirm the $19.78 price" names a figure in order to decline it
        if not any(abs(value - a) <= tolerance for a in allowed):
            blocking.append(f"Price '{m.group(0)}' does not appear in the evidence.")
    ratings = [e["rating"] for e in evidence if e.get("rating") is not None]
    for m in _RATING_VALUE.finditer(text):
        value = float(m.group(1) or m.group(2))
        if value > 5 or _is_denied(text, m.start(), m.end()):
            continue      # "10-star" is not a rating; "it is not rated 4.8" declines the figure
        if not any(abs(value - r) <= 0.051 for r in ratings):
            blocking.append(f"Rating '{m.group(0).strip()}' does not match any rating in the evidence.")
    for m in _REVIEW_CLAIM.finditer(text):
        if not _is_denied(text, m.start(), m.end()):
            blocking.append("Review content is described, but the evidence has only average ratings and rating counts, "
                            "no review text.")
            break
    blocking += unsupported_claims(text, _evidence_text(evidence))

    words = len(answer.get("spoken_summary", "").split())
    if words > MAX_SPOKEN_WORDS:
        minor.append(f"Spoken summary is {words} words (limit {MAX_SPOKEN_WORDS}); it will be trimmed.")
    cited = {r["doc_id"] for r in recs}
    for c in conflicts:
        if c["doc_id"] in cited and not re.search(r"live|current|now|today|2023|catalog price|unavailable|out of stock", text, re.I):
            minor.append(f"Catalog/live {c['field']} conflict for {c['doc_id']} is not mentioned; a caveat will be added.")
            break
    return blocking, minor


def critic_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    answer, evidence = state["answer"], state.get("evidence") or []
    constraints = state["intent"]["constraints"]
    conflicts = state.get("conflicts") or []
    revisions = state.get("revisions", 0)
    known = _history_prices(state.get("history")) | {w["price"] for w in state.get("live_results") or [] if w.get("price") is not None}
    if not evidence:        # near-misses are listed on screen as non-matches; quoting their catalog price is grounded
        known |= {n["price"] for n in state.get("near_misses") or [] if n.get("price") is not None}
    blocking, minor = deterministic_checks(answer, evidence, constraints, conflicts, known,
                                           price_check=state["intent"]["task"] == "price_availability_check")

    llm_crit = None
    if answer.get("source") == "llm":
        shown = {"spoken_summary": answer["spoken_summary"], "caveats": answer["caveats"],
                 "picks": [{"doc_id": r["doc_id"], "title": r["title"], "price": r["price"], "why": r["why"]} for r in answer["recommendations"]]}
        llm_crit = call_structured("critic", Critique, render(
            "critic", transcript=state["transcript"], constraints=json.dumps(constraints),
            history=_fmt_history(state.get("history") or []), tool_calls=_tool_calls_text(state.get("tool_calls") or []),
            conflicts=json.dumps(conflicts) if conflicts else "(none)",
            evidence=_evidence_for_llm(evidence) if evidence else _no_match_evidence(state),
            live_unmatched=_live_unmatched_text(state), answer=json.dumps(shown, indent=1)))
    # The critic tags each issue with its check number, "[1]".."[6]". Only 1-5 (grounding, constraints,
    # conflicts, safety, completeness) send the draft back; [6] quality remarks are recorded, not blocking.
    llm_issues = []
    for issue in (llm_crit.issues if llm_crit else []):
        tag = re.match(r"\s*\[?([1-6])\]?", issue)
        (minor if tag and tag.group(1) == "6" else llm_issues).append(issue)
    if llm_crit and llm_crit.verdict != "revise":
        minor += llm_issues
        llm_issues = []

    issues = blocking + llm_issues
    needs_revision = bool(issues)
    verdict = "revise" if needs_revision and revisions < MAX_REVISIONS else "approve" if not needs_revision else "repair"
    critique = {
        "verdict": verdict,
        "grounded": not blocking and (llm_crit.grounded if llm_crit else True),
        "safe": llm_crit.safe if llm_crit else True,
        "issues": issues, "minor": minor,
        "checks": {"deterministic_blocking": blocking, "llm_critic": llm_crit.model_dump() if llm_crit else None},
    }
    guards = []
    if verdict in ("revise", "repair"):
        who = " and ".join(x for x in ("rule checks" if blocking else "", "the LLM critic" if llm_issues else "") if x)
        guards.append(_guard("rejected", f"Critic ({who})",
                             f"The draft was rejected with {len(issues)} issue(s). "
                             + ("It was sent back to the Answerer for one revision." if verdict == "revise"
                                else "The revision was rejected too, so the wording is replaced by an evidence-only answer.")))
    out: dict = {"critique": critique,
                 "steps": _step("critic", t0, f"verdict={verdict}; {len(blocking)} rule issue(s), {len(llm_issues)} LLM issue(s), {len(minor)} minor",
                                critique=critique, guard=guards)}
    if verdict == "revise":
        out["revisions"] = revisions + 1
    return out


def route_after_critic(state: AgentState) -> str:
    return "answerer" if state["critique"]["verdict"] == "revise" else "finalize"


# ======================================================================= Finalize
def _trim_spoken(text: str, limit: int = MAX_SPOKEN_WORDS) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"https?://\S+|\[?(?:amz20|hh23)-[0-9A-Za-z]+\]?", "", text)).strip()
    if len(text.split()) <= limit:
        return text
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(" ".join(kept + [sentence]).split()) > limit:
            break
        kept.append(sentence)
    return " ".join(kept) if kept else " ".join(text.split()[:limit]).rstrip(",;:") + "."


def finalize_node(state: AgentState) -> dict:
    """Voice response: repair if the critic still objects, add mandatory caveats, build citations."""
    t0 = time.perf_counter()
    answer = dict(state["answer"])
    evidence = state.get("evidence") or []
    constraints = state["intent"]["constraints"]
    critique = dict(state["critique"])
    price_check = state["intent"]["task"] == "price_availability_check"
    repaired = False

    if critique["verdict"] == "repair":
        # The revision did not fix it: fall back to an answer assembled purely from evidence fields.
        ok_ids = {e["doc_id"] for e in evidence}
        safe_state = dict(state)
        safe_state["evidence"] = [e for e in evidence if e["doc_id"] in ok_ids and
                                  (e.get("named") or constraints.get("max_price") is None or e["price"] <= constraints["max_price"]) and
                                  (e.get("named") or constraints.get("min_price") is None or e["price"] >= constraints["min_price"])]
        # Keep the LLM's judgement (which products, in which order, and its still-valid caveats) but not its
        # product wording: the picks are re-described from evidence fields only.
        issues = critique.get("issues") or []
        valid = {e["doc_id"]: e for e in safe_state["evidence"]}
        llm_picks = [r["doc_id"] for r in answer.get("recommendations") or [] if r["doc_id"] in valid]
        unflagged = [d for d in llm_picks if not any(d in issue for issue in issues)]      # a pick the critic named is dropped
        chosen = unflagged or llm_picks
        if chosen:
            safe_state["evidence"] = [valid[d] for d in chosen]
        quoted = [q.lower() for issue in issues for q in re.findall(r"[\"'“]([^\"'”]{8,})[\"'”]", issue)]
        ev_text = _evidence_text(evidence)
        kept_caveats = [c for c in answer.get("caveats") or []
                        if not unsupported_claims(c, ev_text) and not any(q in c.lower() or c.lower() in q for q in quoted)]
        if safe_state["evidence"]:
            answer = _hydrate(_rule_answer(safe_state), safe_state["evidence"])
            answer["caveats"] = kept_caveats
            answer["source"] = "template (repair)"
        else:
            answer = _no_match_answer(state)
            answer["caveats"] += [c for c in kept_caveats if c not in answer["caveats"]]
        remaining, _ = deterministic_checks(answer, evidence, constraints, state.get("conflicts") or [],
                                            _history_prices(state.get("history")), price_check=price_check)
        critique.update(verdict="approve_after_repair", grounded=not remaining, unresolved=remaining,
                        note="LLM draft replaced by an evidence-only template after the revision was still rejected.")
        repaired = True

    recs = answer.get("recommendations") or []
    caveats = list(dict.fromkeys(answer.get("caveats") or []))

    def add(note: str, unless: str | None = None) -> None:
        """Append a mandatory caveat unless it, or one on the same topic (`unless` regex), is already there."""
        if note in caveats or (unless and any(re.search(unless, c, re.I) for c in caveats)):
            return
        caveats.append(note)

    for c in state.get("conflicts") or []:
        if any(r["doc_id"] == c["doc_id"] for r in recs):
            if c["field"] == "price":
                spread = (f" Live prices range from {_money(c['live_range'][0])} to {_money(c['live_range'][1])} across "
                          f"{c.get('live_sources')} shops; the lowest is shown.") if c.get("live_range") else ""
                add(f"Price conflict for {c['title'][:50]}: catalog ({PRICE_SNAPSHOT}) {_money(c['catalog_value'])} vs live {_money(c['live_value'])} "
                    f"({c['live_url']}). The live price is newer.{spread}")
            else:
                add(f"{c['title'][:50]} is listed in the catalog but reported unavailable by the live source ({c['live_url']}).")
    for r in recs:
        if r.get("budget_note"):
            add(f"{r['title'][:50]}: {r['budget_note']}")
    for note in state.get("relaxations") or []:
        add(note)
    for note in state["plan"].get("notes") or []:
        if "not configured" in note:
            add(f"Live price check is not available in this deployment; prices are the {PRICE_SNAPSHOT} catalog snapshot.")
    if recs and not any(r.get("live_price") for r in recs):
        add(f"Prices are from the {PRICE_SNAPSHOT} catalog snapshot and may have changed.", unless=PRICE_SNAPSHOT)
    if any(r.get("brand_inferred") and r.get("brand") for r in recs):
        add("Brands marked * are inferred from the product title (the listing names no brand).", unless=r"\bbrands?\b.*inferred")
    answer["caveats"] = caveats

    rec_ids = {r["doc_id"] for r in recs}
    for e in evidence:      # a catalog match that was skipped because its live price broke the budget
        if e.get("budget_note") and e["doc_id"] not in rec_ids:
            add(f"Not recommended: {e['title'][:50]} [{e['doc_id']}]. {e['budget_note']}")
    answer["caveats"] = caveats

    spoken = _trim_spoken(check_output(answer.get("spoken_summary", "")))
    if any(c["doc_id"] in rec_ids for c in state.get("conflicts") or []) and not re.search(r"live|current|now|2020|2023|catalog|unavailable", spoken, re.I) \
            and len(spoken.split()) <= MAX_SPOKEN_WORDS - 9:
        spoken += " Note the live price differs from the catalog; see screen."
    if recs:      # an unmet constraint must be heard, not only shown
        for note in state.get("spoken_notes") or []:
            key = note.split()[-1].rstrip(".").lower()
            if key not in spoken.lower() and len(spoken.split()) + len(note.split()) <= MAX_SPOKEN_WORDS:
                spoken += " " + note
    answer["spoken_summary"] = spoken

    citations = []
    for r in recs:
        citations.append({"type": "private", "id": r["doc_id"], "title": r["title"], "url": r.get("catalog_url"),
                          "source": CATALOG_NAME})
        if r.get("live_url"):
            citations.append({"type": "live", "id": r["live_url"], "title": r["title"], "url": r["live_url"],
                              "price": r.get("live_price"), "source": "web.search"})

    lines = []
    for i, r in enumerate(recs, 1):
        live = f" | live {_money(r['live_price'])} ({r['live_url']})" if r.get("live_price") else (f" | live source: {r['live_url']}" if r.get("live_url") else "")
        lines.append(f"{i}. {r['title']} - {_money(r['price'])} (catalog, {PRICE_SNAPSHOT}){live} [doc_id: {r['doc_id']}]\n   {r['why']}")
    parts = [spoken]
    if lines:
        parts.append("\n".join(lines))
    if caveats:
        parts.append("Notes:\n" + "\n".join(f"- {c}" for c in caveats))
    if answer.get("follow_up_question") and answer["follow_up_question"] not in spoken:
        parts.append(answer["follow_up_question"])
    final_text = check_output("\n\n".join(parts))

    log_event("agent_runs", {
        "transcript": state.get("transcript"), "intent": state.get("intent"), "plan": state.get("plan"),
        "tool_calls": state.get("tool_calls"), "conflicts": state.get("conflicts"), "citations": citations,
        "critique": critique, "spoken_summary": spoken, "answer_source": answer.get("source"),
    })
    return {"answer": answer, "critique": critique, "final_text": final_text, "spoken_summary": spoken, "citations": citations,
            "steps": _step("finalize", t0, f"{len(recs)} recommendation(s), {len(citations)} citation(s), {len(spoken.split())}-word spoken summary"
                           + (" [repaired]" if repaired else ""), caveats=caveats,
                           guard=[_guard("repaired", "Evidence-only repair (code)",
                                         "The LLM's product wording was discarded; the answer was rebuilt from evidence fields only.")]
                           if repaired else [])}


# ============================================================================ TTS
def tts_node(state: AgentState) -> dict:
    t0 = time.perf_counter()
    if not state.get("synthesize") or not state.get("spoken_summary"):
        return {"steps": _step("tts", t0, "skipped (text-only run)")}
    from ..speech.tts import synthesize
    text, trimmed = state["spoken_summary"], 0
    try:
        audio = synthesize(text)
        # A word cap does not guarantee a duration (model numbers and prices are slow to read), so the
        # real audio length is measured and the summary shortened sentence by sentence until it fits.
        while (audio.get("seconds") or 0) > MAX_SPOKEN_SECONDS and trimmed < 3:
            sentences = re.split(r"(?<=[.!?])\s+", text)
            if len(sentences) < 2:
                break
            text = " ".join(sentences[:-1])
            audio = synthesize(text, audio["path"])
            trimmed += 1
    except Exception as e:  # noqa: BLE001
        return {"audio_out": {"error": str(e)}, "steps": _step("tts", t0, f"TTS failed: {type(e).__name__}", error=str(e))}
    note = f" (shortened {trimmed}x to fit {MAX_SPOKEN_SECONDS} s)" if trimmed else ""
    return {"audio_out": audio, "spoken_summary": text,
            "steps": _step("tts", t0, f"synthesised {audio.get('seconds')} s of speech with {audio['provider']}{note}", **audio)}
