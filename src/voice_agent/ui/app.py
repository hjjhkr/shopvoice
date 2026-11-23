"""Streamlit UI for ShopVoice.

    streamlit run src/voice_agent/ui/app.py

What the page shows for every request:
  * a live pipeline timeline: which node is waiting, running, done, skipped, or where the request was BLOCKED
  * a guardrail checkpoint list: every safety check, whether it passed, flagged, acted or blocked, and at which step
  * the intermediate result of each node as it finishes
  * the answer: spoken summary with audio, recommendation cards, comparison table, notes, citations
"""
from __future__ import annotations

import html
import json
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from voice_agent.config import ROOT, get_settings
from voice_agent.graph.build import mermaid
from voice_agent.graph.nodes import web_available
from voice_agent.llm import llm_status
from voice_agent.pipeline import AgentSession

st.set_page_config(page_title="ShopVoice · voice product discovery", page_icon="🎙", layout="wide",
                   initial_sidebar_state="collapsed")
settings = get_settings()

EXAMPLES = [
    ("Search", "I need a wooden puzzle for a 2 year old under twenty dollars"),
    ("Live price", "What's the current price of the Melissa and Doug safari chunky puzzle?"),
    ("Two asks", "Show me a LEGO set under five dollars, and check the LEGO Classic Creative Suitcase price"),
    ("No match", "Recommend an eco-friendly stainless steel cleaner under fifteen dollars"),
    ("Blocked", "Which cleaners can I mix with bleach to make it stronger?"),
    ("Out of scope", "Great, place the order for the first one and send me the receipt"),
]

# node id -> (label, what it does)
STAGES = {
    "asr": ("ASR", "speech to text"),
    "input_guard": ("Input guard", "ProofAgent Nano scan, before the Router"),
    "router": ("Router", "intent, constraints, safety"),
    "planner": ("Planner", "which tools, which filters"),
    "retriever": ("Retriever", "MCP tool calls, reconcile"),
    "answerer": ("Answerer", "picks and spoken summary"),
    "critic": ("Critic", "grounding and safety review"),
    "finalize": ("Finalize", "caveats, citations, repair"),
    "respond_direct": ("Direct reply", "fixed wording, no tools"),
    "tts": ("TTS", "text to speech"),
}
MAIN = ["asr", "input_guard", "router", "planner", "retriever", "answerer", "critic", "finalize", "tts"]
TOOL_PATH = ["planner", "retriever", "answerer", "critic", "finalize"]

ROUTES_TOOLS = {
    "product_search": "find or recommend products",
    "product_comparison": "compare products or options",
    "price_availability_check": "current price or stock of a product",
    "product_question": "a factual question about a product",
}
ROUTES_DIRECT = {
    "clarification_needed": "too vague to search: ask one question",
    "chitchat": "greeting, thanks",
    "out_of_scope": "orders, carts, anything else",
    "unsafe": "chemical mixing, harm, prompt extraction: refuse",
}

CSS = """
<style>
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:rgba(11,11,11,.10);--surface:#fcfcfb;--wash:#f1f0ec;
  --blue:#2a78d6;--blue-w:rgba(42,120,214,.10);--green:#12885f;--green-w:rgba(27,175,122,.12);
  --red:#c0322f;--red-w:rgba(208,59,59,.10);--amber:#9a6400;--amber-w:rgba(250,178,25,.18);--purple:#6250d6}
#MainMenu,footer,[data-testid="stToolbar"],.stAppDeployButton,[data-testid="stDecoration"]{display:none !important}
header[data-testid="stHeader"]{background:transparent}
.block-container{padding-top:1.4rem;padding-bottom:4rem;max-width:1260px}
html,body,[class*="css"]{font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
h1,h2,h3,h4{letter-spacing:-.01em}

.sv-hero{display:flex;flex-wrap:wrap;gap:14px 24px;align-items:center;justify-content:space-between;
  background:linear-gradient(135deg,#ffffff 0%,#f3f6fb 100%);border:1px solid var(--line);border-radius:18px;padding:18px 22px;margin-bottom:14px}
.sv-hero .logo{width:46px;height:46px;border-radius:13px;background:var(--ink);color:#fff;display:grid;place-items:center;font-size:22px}
.sv-hero .ttl{font-size:1.5rem;font-weight:750;line-height:1.1;letter-spacing:-.02em}
.sv-hero .tag{color:var(--ink2);font-size:.92rem;margin-top:3px}
.sv-pills{display:flex;flex-wrap:wrap;gap:6px}
.sv-pill{font-size:.76rem;border:1px solid var(--line);background:#fff;border-radius:999px;padding:3px 10px;color:var(--ink2);white-space:nowrap}
.sv-pill b{color:var(--ink);font-weight:650}.sv-pill.warn{border-color:#e0a800;background:var(--amber-w)}
.sv-pill i{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:6px;vertical-align:1px}
.sv-pill.warn i{background:#e0a800}

.sv-label{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:700;margin:.2rem 0 .45rem}
.sv-bubble{background:var(--ink);color:#fff;border-radius:16px 16px 4px 16px;padding:11px 16px;max-width:820px;margin:0 0 12px auto;
  font-size:1.02rem;line-height:1.45;width:fit-content}
.sv-bubble small{display:block;opacity:.65;font-size:.72rem;margin-bottom:2px;letter-spacing:.04em;text-transform:uppercase}

/* banners */
.sv-banner{border-radius:14px;padding:14px 18px;margin:0 0 14px;border:1.5px solid;display:grid;grid-template-columns:auto 1fr;gap:4px 14px;align-items:start}
.sv-banner .ic{font-size:1.9rem;line-height:1;grid-row:span 3}
.sv-banner .h{font-size:1.08rem;font-weight:750;letter-spacing:-.01em}
.sv-banner .m{font-size:.9rem}.sv-banner .s{font-size:.82rem;opacity:.85}
.sv-banner code{background:rgba(0,0,0,.07);padding:1px 6px;border-radius:5px;font-size:.82rem}
.sv-banner.block{background:var(--red-w);border-color:var(--red);color:#7c1f1d}
.sv-banner.divert{background:var(--amber-w);border-color:#d49a00;color:#5e3d00}

/* timeline */
.sv-tl{position:relative;margin:.2rem 0 .4rem}
.sv-st{display:grid;grid-template-columns:30px 1fr;gap:10px;position:relative;padding-bottom:12px}
.sv-st:not(:last-child)::before{content:"";position:absolute;left:14px;top:28px;bottom:0;width:2px;background:var(--line)}
.sv-st.done:not(:last-child)::before{background:rgba(27,175,122,.45)}
.sv-dot{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-size:.82rem;font-weight:700;
  border:2px solid var(--line);background:#fff;color:var(--muted);z-index:1}
.sv-st .t{font-weight:650;font-size:.95rem;line-height:1.25;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.sv-st .ms{font-weight:500;font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}
.sv-st .d{font-size:.78rem;color:var(--ink2);line-height:1.3}
.sv-st.done .sv-dot{border-color:#1baf7a;background:var(--green-w);color:var(--green)}
.sv-st.run .sv-dot{border-color:var(--blue);background:var(--blue-w);color:var(--blue);animation:svpulse 1.1s ease-in-out infinite}
.sv-st.run .t{color:var(--blue)}
.sv-st.block .sv-dot{border-color:var(--red);background:var(--red);color:#fff}
.sv-st.block .t{color:var(--red)}
.sv-st.divert .sv-dot{border-color:#d49a00;background:var(--amber-w);color:var(--amber)}
.sv-st.warn .sv-dot{border-color:#d49a00;background:var(--amber-w);color:var(--amber)}
.sv-st.skip{opacity:.55}.sv-st.skip .sv-dot{border-style:dashed}
.sv-st.skip .t{text-decoration:line-through;text-decoration-color:rgba(0,0,0,.35)}
.sv-st.wait{opacity:.6}
@keyframes svpulse{0%,100%{box-shadow:0 0 0 0 rgba(42,120,214,.35)}50%{box-shadow:0 0 0 7px rgba(42,120,214,0)}}
.sv-g{display:inline-block;font-size:.72rem;font-weight:650;border-radius:6px;padding:1px 7px;margin:3px 4px 0 0}
.sv-g.block{background:var(--red);color:#fff}.sv-g.warn{background:var(--amber-w);color:var(--amber);border:1px solid #d49a00}
.sv-g.skip{background:var(--wash);color:var(--muted);border:1px dashed var(--line)}

/* guardrail checkpoints */
.sv-cp{display:grid;grid-template-columns:auto 1fr;gap:6px 10px;align-items:start;padding:7px 0;border-bottom:1px solid var(--line)}
.sv-cp:last-child{border-bottom:0}
.sv-cp .n{font-size:.86rem;font-weight:650;line-height:1.25}
.sv-cp .w{font-size:.74rem;color:var(--muted)}
.sv-cp .x{font-size:.78rem;color:var(--ink2);line-height:1.35;margin-top:2px}
.sv-s{display:inline-block;min-width:88px;text-align:center;font-size:.68rem;font-weight:750;letter-spacing:.04em;border-radius:6px;padding:3px 6px}
.sv-s.ok{background:var(--green-w);color:var(--green)}.sv-s.block{background:var(--red);color:#fff}
.sv-s.warn{background:var(--amber-w);color:var(--amber)}.sv-s.skip{background:var(--wash);color:var(--muted)}

/* answer */
.sv-spoken{font-size:1.18rem;line-height:1.5;font-weight:560;letter-spacing:-.005em;margin:.1rem 0 .5rem}
.sv-pick{border:1px solid var(--line);border-radius:14px;padding:13px 15px;background:#fff;margin-bottom:10px;display:grid;
  grid-template-columns:34px 1fr auto;gap:4px 12px;align-items:start}
.sv-pick.top{border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-w)}
.sv-pick .rk{width:30px;height:30px;border-radius:9px;background:var(--wash);display:grid;place-items:center;font-weight:750;color:var(--ink2)}
.sv-pick.top .rk{background:var(--blue);color:#fff}
.sv-pick .nm{font-weight:650;line-height:1.3}
.sv-pick .why{font-size:.86rem;color:var(--ink2);line-height:1.4;margin-top:3px}
.sv-pick .pr{text-align:right;white-space:nowrap}
.sv-pick .pr b{font-size:1.25rem;letter-spacing:-.02em}
.sv-pick .pr small{display:block;color:var(--muted);font-size:.72rem}
.sv-pick .pr .lv{font-size:.82rem;color:var(--purple);font-weight:650}
.sv-tags{grid-column:2 / span 2;margin-top:5px}
.sv-chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 9px;margin:2px 4px 2px 0;font-size:.78rem;background:var(--wash);color:var(--ink2)}
.sv-chip b{font-weight:650;color:var(--ink)}
.sv-chip.cite{background:#fff;font-family:ui-monospace,Consolas,monospace;font-size:.72rem}
.sv-chip.over{background:var(--red-w);border-color:var(--red);color:var(--red)}
.sv-badge{display:inline-block;border-radius:6px;padding:2px 9px;font-size:.76rem;font-weight:700}
.sv-ok{background:var(--green-w);color:var(--green)}.sv-warn{background:var(--amber-w);color:var(--amber)}
.sv-bad{background:var(--red-w);color:var(--red)}.sv-info{background:var(--blue-w);color:#1f5fae}

/* routes */
.sv-routes{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:.3rem 0 .6rem}
@media (max-width:900px){.sv-routes{grid-template-columns:1fr}}
.sv-rgroup{border:1px dashed var(--line);border-radius:12px;padding:9px 11px;background:#fff}
.sv-rgroup.active{border-style:solid;border-color:var(--blue)}
.sv-rgroup.active.bad{border-color:var(--red)}
.sv-rgroup .gl{font-size:.76rem;font-weight:700;color:var(--ink2);margin-bottom:5px}
.sv-route{display:flex;gap:8px;align-items:baseline;border:1px solid var(--line);border-radius:8px;padding:3px 9px;margin:4px 0;font-size:.82rem;opacity:.5}
.sv-route code{font-size:.78rem;background:none;padding:0;color:inherit}
.sv-route span{color:var(--ink2)}
.sv-routes.idle .sv-route{opacity:1}
.sv-routes.compact{grid-template-columns:1fr;gap:8px}
.sv-routes.compact .sv-route{display:inline-flex;margin:3px 5px 3px 0;padding:2px 9px}
.sv-routes.compact .sv-route span{display:none}
.sv-route.sel{opacity:1;border-color:var(--blue);background:var(--blue-w);font-weight:650}
.sv-route.sel.bad{border-color:var(--red);background:var(--red-w)}
.sv-route.sel::before{content:"✓";color:var(--blue)}.sv-route.sel.bad::before{content:"🛡"}

/* meters (evaluation tab) */
.sv-meter{display:grid;grid-template-columns:200px 1fr 56px 110px;gap:10px;align-items:center;padding:5px 0;font-size:.9rem}
.sv-meter .trk{height:9px;border-radius:5px;background:#dfe9f7;overflow:hidden}
.sv-meter .fill{height:100%;border-radius:5px;background:var(--blue)}
.sv-meter .v{text-align:right;font-variant-numeric:tabular-nums;font-weight:650}
div[data-testid="stVerticalBlockBorderWrapper"]{border-radius:14px !important;background:var(--surface)}
div[data-testid="stExpander"] details{border-radius:12px !important;background:var(--surface)}
.stButton>button{border-radius:10px}
</style>
"""


# ------------------------------------------------------------------ small helpers
def money(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"${v:,.2f}"


def secs(ms) -> str:
    return "" if ms is None else (f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s")


def esc(v) -> str:
    return html.escape(str(v))


def chip(label: str, value, cls: str = "") -> str:
    return f'<span class="sv-chip {cls}">{esc(label)}: <b>{esc(value)}</b></span>'


def badge(text: str, kind: str = "info") -> str:
    return f'<span class="sv-badge sv-{kind}">{esc(text)}</span>'


def last_step(update: dict) -> dict:
    return (update.get("steps") or [{}])[-1]


def guards_of(update: dict) -> list[dict]:
    return (last_step(update).get("detail") or {}).get("guard") or []


# ------------------------------------------------------------------ what happened: status, block, checkpoints
def node_status(events: list[tuple[str, dict]], running: str | None = None) -> dict[str, dict]:
    """Per node: state (done / block / divert / warn / skip / run / wait), elapsed ms, run count, guard events."""
    status: dict[str, dict] = {}
    for node, update in events:
        step = last_step(update)
        prev = status.get(node, {"ms": 0, "n": 0, "guards": []})
        gs = prev["guards"] + guards_of(update)
        state = "done"
        if any(g["action"] == "blocked" for g in gs) and node in ("router", "input_guard"):
            state = "block"
        elif any(g["action"] == "diverted" for g in gs):
            state = "divert"
        elif gs:
            state = "warn"
        if "failed" in (step.get("summary") or ""):
            state = "warn"
        status[node] = {"state": state, "ms": prev["ms"] + (step.get("ms") or 0), "n": prev["n"] + 1, "guards": gs}
    if "respond_direct" in status or (running == "respond_direct"):
        bypassed = TOOL_PATH + (["router"] if status.get("input_guard", {}).get("state") == "block" else [])
        for node in bypassed:                        # the tool path (and, after an input-guard stop, the Router) was bypassed
            status.setdefault(node, {"state": "skip", "ms": None, "n": 0, "guards": []})
    if running:
        status[running] = {**status.get(running, {"ms": None, "n": 0, "guards": []}), "state": "run"}
    return status


def block_info(events: list[tuple[str, dict]]) -> dict | None:
    """The guard event that stopped or diverted the whole request, with the step that raised it."""
    for i, (node, update) in enumerate(events, 1):
        for g in guards_of(update):
            if (node == "router" and g["action"] in ("blocked", "diverted")) or (node == "input_guard" and g["action"] == "blocked"):
                return {**g, "step": i, "node": node}
    return None


GUARD_LABEL = {"blocked": ("🛡 blocked", "block"), "diverted": ("↪ diverted", "warn"), "flagged": ("⚑ flagged", "warn"),
               "policy": ("⚙ policy applied", "warn"), "sanitised": ("✂ sanitised", "warn"), "constraint": ("≠ constraint noted", "warn"),
               "rejected": ("✗ draft rejected", "warn"), "repaired": ("🛠 repaired", "warn")}


def timeline_html(status: dict[str, dict]) -> str:
    path = list(MAIN)
    if "respond_direct" in status:
        path.insert(path.index("tts"), "respond_direct")
    icons = {"done": "✓", "run": "●", "block": "🛡", "divert": "↪", "warn": "!", "skip": "–", "wait": ""}
    out = ['<div class="sv-tl">']
    for i, node in enumerate(path, 1):
        label, desc = STAGES[node]
        s = status.get(node, {"state": "wait"})
        state = s["state"]
        right = {"run": "running…", "skip": "skipped, not executed", "wait": "waiting"}.get(state, secs(s.get("ms")))
        if s.get("n", 0) > 1:
            right += f" · ran ×{s['n']}"
        tags = ""
        seen = set()
        for g in s.get("guards") or []:
            text, cls = GUARD_LABEL.get(g["action"], (g["action"], "warn"))
            key = (text, g.get("category"))
            if key in seen:
                continue
            seen.add(key)
            tags += f'<span class="sv-g {cls}" title="{esc(g["mechanism"])}: {esc(g["detail"])}">{text}' \
                    f'{" · " + esc(g["category"]) if g.get("category") else ""}</span>'
        out.append(f'<div class="sv-st {state}"><div class="sv-dot">{icons.get(state) or i}</div><div>'
                   f'<div class="t">{i}. {label}<span class="ms">{right}</span></div><div class="d">{desc}</div>{tags}</div></div>')
    out.append("</div>")
    return "".join(out)


def banner_html(info: dict | None) -> str:
    if not info:
        return ""
    label = STAGES[info["node"]][0]
    if info["action"] == "blocked":
        return (f'<div class="sv-banner block"><div class="ic">🛡</div>'
                f'<div class="h">Request blocked at step {info["step"]} · {label}</div>'
                f'<div class="m">Blocked by <b>{esc(info["mechanism"])}</b>'
                f'{" · category <code>" + esc(info["category"]) + "</code>" if info.get("category") else ""}. {esc(info["detail"])}</div>'
                f'<div class="s">Not executed because of this block: {"Router (no LLM call), " if info["node"] == "input_guard" else ""}'
                f'Planner, Retriever (no MCP tool call), Answerer, Critic, Finalize. '
                f'The reply below is fixed wording.</div></div>')
    return (f'<div class="sv-banner divert"><div class="ic">↪</div>'
            f'<div class="h">Request diverted at step {info["step"]} · {label}</div>'
            f'<div class="m">Route <code>{esc(info.get("category") or "direct")}</code> · {esc(info["detail"])}</div>'
            f'<div class="s">Not executed: Planner, Retriever (no MCP tool call), Answerer, Critic, Finalize.</div></div>')


def input_guard_row(scan: dict, stopped: bool) -> tuple[str, str, str, str, str]:
    """Checkpoint row for the ProofAgent Nano scan. Only a real API response is shown as a ProofAgent verdict."""
    src, action, decision = scan.get("source"), scan.get("action"), scan.get("decision")
    step = "1 · Input guard"
    if src == "disabled":
        return (step, "ProofAgent Nano input scan", "OFF", "skip",
                f"{scan.get('note') or 'No scan was made.'} ShopVoice's own pattern guardrails still run in the Router.")
    if src == "error":
        return (step, "ProofAgent Nano input scan", "SCAN FAILED", "block" if stopped else "warn",
                f"No ProofAgent verdict: {scan.get('error')}. {scan.get('note', '')}")
    kind = {"pass": "ok", "mask": "warn", "review": "block", "block": "block"}.get(decision, "warn")
    facts = [f"decision {decision}", f"severity {scan.get('severity')}"]
    if scan.get("top_risk"):
        facts.append(f"top risk {scan['top_risk']}")
    if scan.get("request_id"):
        facts.append(f"request {scan['request_id']}")
    if scan.get("latency_ms") is not None:
        facts.append(f"{scan['latency_ms']} ms")
    name = "ProofAgent Nano input scan (live API)" if src == "proofagent_nano" else "MOCKED ProofAgent response (test only, not a real scan)"
    return (step, name, f"{'MOCK ' if src == 'mock' else ''}{action}", kind,
            " · ".join(facts) + (f". {scan['note']}" if scan.get("note") else ""))


def checkpoints(events: list[tuple[str, dict]], result: dict) -> list[dict]:
    """Every guardrail check in pipeline order with its outcome: ok / warn / block / skip (not reached)."""
    by: dict[str, list[dict]] = {}
    for node, update in events:
        by.setdefault(node, []).extend(guards_of(update))
    ran = {node for node, _ in events}
    rg = by.get("router", [])
    input_block = next((g for g in rg if g["action"] == "blocked" and g["mechanism"].startswith("Input guardrail")), None)
    flagged = next((g for g in rg if g["action"] == "flagged"), None)
    cls_block = next((g for g in rg if g["action"] == "blocked" and g["mechanism"].startswith("Router safety")), None)
    diverted = next((g for g in rg if g["action"] == "diverted"), None)
    stopped = bool(input_block or cls_block or diverted)
    rows = []

    def row(step: str, name: str, status: str, kind: str, detail: str) -> None:
        rows.append({"step": step, "name": name, "status": status, "kind": kind, "detail": detail})

    guard_stop = any(g["action"] == "blocked" for g in by.get("input_guard", []))
    if "input_guard" in ran:
        row(*input_guard_row(result.get("input_scan") or {}, guard_stop))
    if guard_stop:
        for name in ("Input guardrail (patterns, before any LLM)", "Safety classification", "Scope check (route)"):
            row("2 · Router", name, "NOT REACHED", "skip", "The ProofAgent input guard stopped the request before the Router.")
        stopped = True
    elif "router" not in ran:
        return rows
    else:
        if input_block:
            row("2 · Router", "Input guardrail (patterns, before any LLM)", "BLOCKED", "block",
                f"Matched a {input_block['category']} pattern. {input_block['detail']}")
        elif flagged:
            row("2 · Router", "Input guardrail (patterns, before any LLM)", "FLAGGED", "warn", flagged["detail"])
        else:
            row("2 · Router", "Input guardrail (patterns, before any LLM)", "PASSED", "ok", "No harm, chemical-mixing or injection pattern matched.")
        if input_block:
            row("2 · Router", "Safety classification", "NOT REACHED", "skip", "The input guardrail had already blocked the request.")
        elif cls_block:
            row("2 · Router", "Safety classification", "BLOCKED", "block", f"Category {cls_block['category']}. {cls_block['detail']}")
        else:
            row("2 · Router", "Safety classification", "PASSED", "ok", "Safety flag: none.")
        if input_block or cls_block:
            row("2 · Router", "Scope check (route)", "NOT REACHED", "skip", "Blocked before routing.")
        elif diverted:
            row("2 · Router", "Scope check (route)", "DIVERTED", "warn", f"Route {diverted['category']}. {diverted['detail']}")
        else:
            row("2 · Router", "Scope check (route)", "IN SCOPE", "ok", f"Route {(result.get('intent') or {}).get('task', '')}: continues to the tool path.")

    def later(step: str, node: str, name: str, actions: tuple, ok_text: str, ok_label: str = "PASSED") -> None:
        if stopped:
            row(step, name, "NOT REACHED", "skip", "The request was stopped before this step.")
            return
        if node not in ran:
            row(step, name, "PENDING", "skip", "This step has not run yet.")
            return
        hits = [g for g in by.get(node, []) if g["action"] in actions]
        if not hits:
            row(step, name, ok_label, "ok", ok_text)
            return
        blocked = any(g["action"] == "blocked" for g in hits)
        label = {"policy": "ADJUSTED", "sanitised": "SANITISED", "constraint": "NOTED", "rejected": "REJECTED",
                 "repaired": "REPAIRED", "blocked": "CALL BLOCKED"}[hits[0]["action"]]
        row(step, name, label, "block" if blocked else "warn", " ".join(dict.fromkeys(g["detail"] for g in hits)))

    calls = result.get("tool_calls") or []
    later("3 · Planner", "planner", "Tool policy (which tools may be planned)", ("policy",),
          "Plan accepted as written: catalog search present; web search only if live info was asked.")
    later("4 · Retriever", "retriever", "Tool allow-list and approved plan", ("blocked",),
          f"{len(calls)} call(s), all to allowed tools, with hard constraints re-applied in code.")
    later("4 · Retriever", "retriever", "Tool-output sanitiser", ("sanitised",),
          "No instruction-like text found in tool results.", "CLEAN")
    later("4 · Retriever", "retriever", "Constraint verification (budget, brand, type, material)", ("constraint",),
          "Every kept product satisfies the stated constraints.")
    later("6 · Critic", "critic", "Grounding and safety review of the draft", ("rejected",),
          "Draft approved: cited, within constraints, no unsupported claims.", "APPROVED")
    later("7 · Finalize", "finalize", "Evidence-only repair", ("repaired",),
          "Not needed: the approved draft was used.", "NOT NEEDED")
    done = "finalize" in ran or "respond_direct" in ran
    row("7 · Finalize" if "finalize" in ran else "Direct reply", "Output redaction (secrets never shown or logged)",
        "APPLIED" if done else "NOT REACHED", "ok" if done else "skip",
        "Every reply and log line passes the redactor." if done else "Not run yet.")
    return rows


def checkpoints_html(rows: list[dict]) -> str:
    if not rows:
        return '<div class="sv-cp"><div></div><div class="w">Waiting for the Router…</div></div>'
    return "".join(
        f'<div class="sv-cp"><div><span class="sv-s {r["kind"]}">{r["status"]}</span></div>'
        f'<div><div class="n">{esc(r["name"])}</div><div class="w">step {esc(r["step"])}</div><div class="x">{esc(r["detail"])}</div></div></div>'
        for r in rows)


def guard_headline(rows: list[dict]) -> str:
    blocked = sum(r["kind"] == "block" for r in rows)
    acted = sum(r["kind"] == "warn" for r in rows)
    if blocked:
        return f"{blocked} blocked · {acted} acted"
    return f"{acted} acted, nothing blocked" if acted else "all checks passed"


# ------------------------------------------------------------------ routes
def routes_html(chosen: str, compact: bool = False) -> str:
    """All eight routes grouped by destination. `compact` shows them as pills (for narrow cards)."""
    def group(title: str, routes: dict[str, str]) -> str:
        rows = "".join(
            f'<div class="sv-route{" sel" if key == chosen else ""}{" bad" if key == chosen == "unsafe" else ""}">'
            f"<code>{key}</code><span>{esc(desc)}</span></div>" for key, desc in routes.items())
        active = (" active" + (" bad" if chosen == "unsafe" else "")) if chosen in routes else ""
        return f'<div class="sv-rgroup{active}"><div class="gl">{title}</div>{rows}</div>'
    meaning = {**ROUTES_TOOLS, **ROUTES_DIRECT}.get(chosen, "")
    return (f'<div class="sv-routes{"" if chosen else " idle"}{" compact" if compact else ""}">'
            + group("Route A → Planner → Retriever (MCP tools) → Answerer → Critic", ROUTES_TOOLS)
            + group("Route B → Direct reply (fixed wording, no tools)", ROUTES_DIRECT) + "</div>"
            + (f"<div style='font-size:.84rem;color:#52514e;margin:-.2rem 0 .5rem'><code>{chosen}</code> = {esc(meaning)}</div>"
               if compact and meaning else ""))


# ------------------------------------------------------------------ node cards (intermediate results)
def card(index: int, node: str, update: dict, state: dict) -> None:
    step = last_step(update)
    detail = step.get("detail") or {}
    label, desc = STAGES.get(node, (node, ""))
    with st.container(border=True):
        tags = "".join(f' <span class="sv-g {GUARD_LABEL.get(g["action"], ("", "warn"))[1]}">{GUARD_LABEL.get(g["action"], (g["action"],))[0]}</span>'
                       for g in detail.get("guard") or [])
        st.markdown(f"**{index}. {label}** &nbsp;<span style='color:#898781'>{secs(step.get('ms'))}</span>{tags}", unsafe_allow_html=True)
        for g in detail.get("guard") or []:
            st.markdown(f"<div style='font-size:.84rem;color:#52514e;margin:-.3rem 0 .4rem'>↳ <b>{esc(g['mechanism'])}</b>: {esc(g['detail'])}</div>",
                        unsafe_allow_html=True)
        try:
            RENDERERS.get(node, lambda *a: None)(update, detail, state)
        except Exception as e:  # noqa: BLE001 - a rendering problem must never hide the result
            st.caption(f"(could not render details: {e})")
        with st.popover("Raw output of this node"):          # a popover, so cards can sit inside an expander
            st.json({k: v for k, v in update.items() if k != "steps"} or detail, expanded=1)


def card_asr(update, detail, state):
    asr = update.get("asr") or {}
    if asr.get("error"):
        st.error(f"Speech recognition failed: {asr['error']}")
    elif asr:
        st.markdown(f"> {asr.get('text') or '*(nothing recognised)*'}")
        st.caption(f"{asr.get('provider')}:{asr.get('model')} · audio {asr.get('duration')} s · language {asr.get('language')}")
        if asr.get("segments"):
            st.dataframe(pd.DataFrame(asr["segments"]), hide_index=True, use_container_width=True)
    else:
        st.caption("Typed input: speech recognition was skipped.")


def card_input_guard(update, detail, state):
    scan = detail.get("scan") or {}
    src = scan.get("source")
    kind = {"proofagent_nano": "ok", "mock": "warn", "disabled": "info", "error": "bad"}.get(src, "info")
    st.markdown(badge(detail.get("source_label", src or "?"), kind), unsafe_allow_html=True)
    if src in ("proofagent_nano", "mock"):
        st.markdown(" ".join([chip("ProofAgent action", scan.get("action")), chip("ShopVoice decision", scan.get("decision")),
                              chip("severity", scan.get("severity")), chip("model", scan.get("model"))]), unsafe_allow_html=True)
        if scan.get("risks"):
            st.markdown(" ".join(chip("risk", r) for r in scan["risks"]), unsafe_allow_html=True)
        if scan.get("summary"):
            st.caption(scan["summary"])
        st.caption(f"request id {scan.get('request_id') or 'n/a'} · HTTP {scan.get('http_status')} · {scan.get('latency_ms')} ms")
    else:
        st.caption(f"{scan.get('error') or ''} {scan.get('note') or ''}".strip()
                   + " This is not a ProofAgent verdict.")
    if scan.get("masked"):
        st.caption("Personal data was masked: the Router received the text with placeholders.")


def card_router(update, detail, state):
    intent = update.get("intent") or {}
    c = intent.get("constraints") or {}
    st.markdown("<div class='sv-label'>Route chosen · 1 of 8</div>", unsafe_allow_html=True)
    st.markdown(routes_html(intent.get("task", ""), compact=True), unsafe_allow_html=True)
    budget = "–"
    if c.get("min_price") is not None or c.get("max_price") is not None:
        budget = f"{money(c['min_price']) if c.get('min_price') is not None else '$0'} – {money(c['max_price']) if c.get('max_price') is not None else 'any'}"
    chips = [chip("budget", budget)]
    for key, name in (("product_type", "product"), ("brand", "brand"), ("material", "material"), ("category", "category"),
                      ("min_rating", "min rating")):
        if c.get(key):
            chips.append(chip(name, c[key]))
    chips += [chip("prefers", p) for p in c.get("preferences") or []]
    chips += [chip("named product", n) for n in intent.get("named_products") or []]
    chips.append(chip("wants live price", "yes" if intent.get("wants_live_info") else "no"))
    chips.append(chip("decided by", detail.get("source", "?")))
    st.markdown("<div class='sv-label'>Extracted from the request</div>" + " ".join(chips), unsafe_allow_html=True)
    if intent.get("search_query"):
        st.caption(f"Search query: “{intent['search_query']}”")
    for note in detail.get("notes") or []:
        st.caption(f"↳ {note}")


def card_planner(update, detail, state):
    plan = update.get("plan") or {}
    rows = [{"tool": s["tool"], "query": s["query"],
             "filters": ", ".join(f"{k}={s[k]}" for k in ("max_price", "min_price", "brand", "category", "min_rating") if s.get(k) not in (None, "")) or "–",
             "purpose": s.get("purpose") or ""} for s in plan.get("steps") or []]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.markdown(" ".join([chip("tools discovered via MCP", ", ".join(detail.get("discovered_tools") or [])),
                          chip("planned by", detail.get("source", "?")),
                          chip("uses live web", "yes" if plan.get("use_web") else "no")]), unsafe_allow_html=True)
    if plan.get("comparison_criteria"):
        st.caption("Ranking criteria: " + " → ".join(plan["comparison_criteria"]))
    if plan.get("rationale"):
        st.caption(f"Why: {plan['rationale']}")


def card_retriever(update, detail, state):
    calls = update.get("tool_calls") or []
    if calls:
        st.markdown("<div class='sv-label'>MCP tool calls</div>", unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "tool": c["tool"], "issued by": c["origin"],
            "arguments": json.dumps({k: v for k, v in c["args"].items() if k != "top_k"}),
            "results": c["count"], "ms": c.get("ms"), "error": c.get("error") or ""} for c in calls]),
            hide_index=True, use_container_width=True)
    evidence = update.get("evidence") or []
    if evidence:
        st.markdown(f"<div class='sv-label'>Evidence kept · {len(evidence)} · the only product facts the Answerer may use</div>",
                    unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "doc_id": e["doc_id"], "title": e["title"][:70], "price": money(e.get("price")),
            "live price": money((e.get("live") or {}).get("price")) if e.get("live") else "–",
            "age": e.get("recommended_age") or "–", "brand": e.get("brand") or "–",
            "named by user": "yes" if e.get("named") else "", "note": e.get("budget_note") or ""} for e in evidence]),
            hide_index=True, use_container_width=True)
    elif update.get("near_misses"):
        st.info("No product matched closely enough. Closest items (not matches): "
                + "; ".join(n["title"][:50] for n in update["near_misses"]))
    for c in update.get("conflicts") or []:
        st.warning(f"Conflict on {c['field']} · {c['title'][:50]}: catalog {c['catalog_value']} vs live {c['live_value']}")
    st.markdown(chip("mode", detail.get("mode", "?")), unsafe_allow_html=True)


def card_answerer(update, detail, state):
    answer = update.get("answer") or {}
    st.markdown(f"*Draft spoken summary:* {answer.get('spoken_summary', '')}")
    for r in answer.get("recommendations") or []:
        st.markdown(f"- **{r['title'][:70]}** ({money(r.get('price'))}) `{r['doc_id']}` · {r['why']}")
    st.markdown(chip("written by", answer.get("source", "?")), unsafe_allow_html=True)


def card_critic(update, detail, state):
    crit = update.get("critique") or {}
    verdict = crit.get("verdict", "?")
    kind = {"approve": "ok", "revise": "warn", "repair": "bad"}.get(verdict, "info")
    meaning = {"approve": "approved", "revise": "sent back to the Answerer", "repair": "still rejected → evidence-only repair"}.get(verdict, verdict)
    st.markdown(" ".join([badge(meaning, kind), chip("grounded", crit.get("grounded")), chip("safe", crit.get("safe"))]),
                unsafe_allow_html=True)
    for issue in crit.get("issues") or []:
        st.markdown(f"- ❗ {issue}")
    for issue in crit.get("minor") or []:
        st.caption(f"ℹ️ {issue}")


def card_finalize(update, detail, state):
    answer = update.get("answer") or {}
    st.markdown(f"*Final spoken summary ({len((update.get('spoken_summary') or '').split())} words):* {update.get('spoken_summary', '')}")
    st.markdown(" ".join([chip("recommendations", len(answer.get("recommendations") or [])),
                          chip("citations", len(update.get("citations") or [])),
                          chip("notes added", len(answer.get("caveats") or [])),
                          chip("critic", (update.get("critique") or {}).get("verdict", "?"))]), unsafe_allow_html=True)


def card_direct(update, detail, state):
    st.markdown(f"> {update.get('final_text', '')}")
    st.caption("Fixed wording. No tool was called and there are no product claims to verify.")


def card_tts(update, detail, state):
    audio = update.get("audio_out") or {}
    if audio.get("error"):
        st.warning(f"Speech synthesis failed: {audio['error']}")
    elif audio.get("path"):
        st.caption(f"{audio.get('provider')} · {audio.get('seconds')} s of speech (limit 15 s)")
    else:
        st.caption("Speech synthesis was switched off for this request.")


RENDERERS = {"asr": card_asr, "input_guard": card_input_guard, "router": card_router, "planner": card_planner, "retriever": card_retriever,
             "answerer": card_answerer, "critic": card_critic, "finalize": card_finalize,
             "respond_direct": card_direct, "tts": card_tts}


# ------------------------------------------------------------------ answer
def picks_html(recs: list[dict], max_price) -> str:
    out = []
    for i, r in enumerate(recs, 1):
        tags = []
        if r.get("recommended_age"):
            tags.append(chip("age", r["recommended_age"]))
        if r.get("ingredients"):
            tags.append(chip("materials", r["ingredients"]))
        if r.get("brand"):
            tags.append(chip("brand", r["brand"] + (" (inferred)" if r.get("brand_inferred") else "")))
        tags.append(chip("rating", f"{r['rating']:.1f}" if r.get("rating") is not None else "n/a"))
        if r.get("budget_note"):
            tags.append('<span class="sv-chip over">over budget</span>')
        tags.append(f'<span class="sv-chip cite">📦 {esc(r["doc_id"])}</span>')
        if r.get("live_url"):
            tags.append(f'<span class="sv-chip cite">🌐 {esc(r["live_url"].split("/")[2])}</span>')
        live = f'<div class="lv">live {money(r["live_price"])}</div>' if r.get("live_price") else ""
        out.append(f'<div class="sv-pick{" top" if i == 1 else ""}"><div class="rk">{i}</div>'
                   f'<div><div class="nm">{esc(r["title"])}</div><div class="why">{esc(r["why"])}</div></div>'
                   f'<div class="pr"><b>{money(r.get("price"))}</b><small>catalog, Jan 2020</small>{live}</div>'
                   f'<div class="sv-tags">{"".join(tags)}</div></div>')
    return "".join(out)


def render_answer(result: dict, autoplay: bool) -> None:
    answer = result.get("answer") or {}
    recs = answer.get("recommendations") or []
    critique = result.get("critique") or {}
    verdict = critique.get("verdict")

    st.markdown("<div class='sv-label'>🔊 Spoken answer</div>"
                f"<div class='sv-spoken'>{esc(result.get('spoken_summary', ''))}</div>", unsafe_allow_html=True)
    audio = result.get("audio_out") or {}
    if audio.get("path") and Path(audio["path"]).exists():
        st.audio(audio["path"], autoplay=autoplay)
    if "direct reply" in (critique.get("note") or ""):          # the Critic never ran on this path
        review = badge("fixed reply · critic not run (no product claims)", "info") + " "
    else:
        review = (badge({"approve": "critic approved", "approve_after_repair": "approved after repair"}.get(verdict, str(verdict)),
                        "ok" if verdict == "approve" else "warn")
                  + " " + chip("grounded", critique.get("grounded")) + chip("safe", critique.get("safe")))
    st.markdown(review
                + chip("spoken", f"{len((result.get('spoken_summary') or '').split())} words"
                       + (f" · {audio['seconds']} s" if audio.get("seconds") else "")), unsafe_allow_html=True)

    if not recs and not answer.get("caveats"):
        return
    t_picks, t_table, t_notes, t_cites = st.tabs(
        [f"Recommendations ({len(recs)})", "Comparison table", f"Notes ({len(answer.get('caveats') or [])})",
         f"Citations ({len(result.get('citations') or [])})"])
    with t_picks:
        if recs:
            st.markdown(picks_html(recs, (result.get("intent") or {}).get("constraints", {}).get("max_price")), unsafe_allow_html=True)
        else:
            st.info("No product is recommended for this request. See Notes for the closest items and why they are not matches.")
        if answer.get("follow_up_question"):
            st.markdown(f"💬 *{answer['follow_up_question']}*")
    with t_table:
        if recs:
            rows = [{
                "#": i, "Product": r["title"],
                "Price (catalog, 2020)": money(r.get("price")),
                "Live price": money(r.get("live_price")) if r.get("live_price") else "-",
                "Rating": f"{r['rating']:.1f}" if r.get("rating") is not None else "n/a",
                "Ingredients / materials": r.get("ingredients") or "n/a",
                "Recommended age": r.get("recommended_age") or "n/a",
                "Brand": (r.get("brand") or "n/a") + (" *" if r.get("brand_inferred") and r.get("brand") else ""),
                "Why": r["why"],
                "Source": "Catalog" + (" + live web" if r.get("live_url") else ""),
                "Citation (doc_id)": r["doc_id"],
                "Live source": r.get("live_url"),
            } for i, r in enumerate(recs, 1)]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                         column_config={"Live source": st.column_config.LinkColumn(display_text="open"),
                                        "Why": st.column_config.TextColumn(width="large")})
            st.caption("* brand inferred from the product title. Ratings are n/a because the 2020 dataset has none.")
        else:
            st.caption("Nothing to compare.")
    with t_notes:
        for c in answer.get("caveats") or []:
            st.markdown(f"- {c}")
        if not answer.get("caveats"):
            st.caption("No notes.")
    with t_cites:
        citations = result.get("citations") or []
        for c in citations:
            if c["type"] == "private":
                link = f" · [catalog listing]({c['url']})" if c.get("url") else ""
                st.markdown(f"- 📦 private doc `{c['id']}` · {c['title'][:70]}{link}")
            else:
                st.markdown(f"- 🌐 live source [{c['url']}]({c['url']}) · {money(c.get('price'))}")
        other_live = result.get("live_results") or []
        if other_live:
            st.caption("Live results that matched no catalog product:")
            for w in other_live:
                st.markdown(f"- [{w['title'][:80]}]({w['url']}) · {money(w.get('price'))}")
        if not citations and not other_live:
            st.caption("No citations: no product claim was made.")


def render_side(status: dict, rows: list[dict], headline_extra: str = "") -> None:
    with st.container(border=True):
        st.markdown(f"<div class='sv-label'>Pipeline{headline_extra}</div>" + timeline_html(status), unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown(f"<div class='sv-label'>🛡 Guardrail checkpoints · {guard_headline(rows) if rows else 'pending'}</div>"
                    + checkpoints_html(rows), unsafe_allow_html=True)


# ------------------------------------------------------------------ page
if "session" not in st.session_state:
    st.session_state.session = AgentSession()
    st.session_state.turns = []          # [{"result": final state, "events": [(node, update), ...]}], newest last
    st.session_state.last_audio_id = None
    st.session_state.autoplay_turn = None

st.markdown(CSS, unsafe_allow_html=True)

llm_ok, llm_text = llm_status()
web_ok = web_available()
pills = [
    f'<span class="sv-pill{"" if llm_ok else " warn"}"><i></i>LLM <b>{esc(llm_text if llm_ok else "rule-based fallback")}</b></span>',
    f'<span class="sv-pill"><i></i>ASR <b>{esc(settings.asr_provider)} / {esc(settings.asr_model)}</b></span>',
    f'<span class="sv-pill"><i></i>TTS <b>{esc(settings.tts_provider)}</b></span>',
    f'<span class="sv-pill{"" if web_ok else " warn"}"><i></i>web.search <b>{esc(settings.web_provider)}</b></span>',
    f'<span class="sv-pill"><i></i>MCP <b>{esc(settings.mcp_transport)}</b></span>',
    f'<span class="sv-pill{"" if settings.guard_mode == "on" and settings.nano_key else " warn"}"><i></i>ProofAgent input guard <b>'
    + esc("on (Nano API)" if settings.guard_mode == "on" and settings.nano_key else
          "on, NO KEY (scans fail)" if settings.guard_mode == "on" else "off") + "</b></span>",
]
st.markdown(
    '<div class="sv-hero"><div style="display:flex;gap:14px;align-items:center"><div class="logo">🎙</div>'
    '<div><div class="ttl">ShopVoice</div><div class="tag">Voice-to-voice product discovery with grounded, cited answers. '
    'Every step and every guardrail is shown.</div></div></div>'
    f'<div class="sv-pills">{"".join(pills)}</div></div>', unsafe_allow_html=True)

with st.sidebar:
    st.header("Settings")
    speak = st.toggle("Speak the answer (TTS)", value=True)
    if st.button("New conversation", use_container_width=True):
        st.session_state.session.reset()
        st.session_state.turns = []
        st.rerun()
    st.caption("Providers are set in `.env`; no code change is needed to swap them.")
    with st.expander("LangGraph flow (Mermaid)"):
        st.code(mermaid(), language="text")

tab_chat, tab_eval = st.tabs(["Assistant", "Evaluation evidence"])

with tab_chat:
    # -------------------------------------------------------------- composer
    request_audio: str | None = None
    request_text: str | None = None
    with st.container(border=True):
        col_mic, col_text = st.columns([1, 2.2], gap="large")
        with col_mic:
            st.markdown("<div class='sv-label'>Speak</div>", unsafe_allow_html=True)
            recording = st.audio_input("Record your request", label_visibility="collapsed")
            upload = st.file_uploader("or upload a WAV / MP3", type=["wav", "mp3", "m4a"], label_visibility="collapsed")
            clip = recording or upload
            if clip is not None:
                clip_id = f"{getattr(clip, 'name', 'rec')}-{clip.size}"
                if clip_id != st.session_state.last_audio_id:      # process each new clip once
                    st.session_state.last_audio_id = clip_id
                    suffix = Path(getattr(clip, "name", "rec.wav")).suffix or ".wav"
                    settings.audio_dir.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=settings.audio_dir) as f:
                        f.write(clip.getvalue())
                        request_audio = f.name
        with col_text:
            st.markdown("<div class='sv-label'>or type</div>", unsafe_allow_html=True)
            typed = st.chat_input("Ask for a product, a comparison, or a current price…")
            if typed:
                request_text = typed
            st.markdown("<div class='sv-label' style='margin-top:.8rem'>Try an example</div>", unsafe_allow_html=True)
            cols = st.columns(2)
            for i, (tag, ex) in enumerate(EXAMPLES):
                if cols[i % 2].button(f"**{tag}** · {ex}", key=f"ex{i}", use_container_width=True):
                    request_text = ex

    # -------------------------------------------------------------- live run
    if request_audio or request_text:
        st.markdown(f"<div class='sv-bubble'><small>You</small>{esc(request_text or '🎙 audio clip')}</div>", unsafe_allow_html=True)
        banner_slot = st.empty()
        left, right = st.columns([1.55, 1], gap="large")
        with right:
            side_slot = st.empty()
        with left:
            st.markdown("<div class='sv-label'>Working · intermediate results appear as each step finishes</div>", unsafe_allow_html=True)
            feed = st.container()
        events: list[tuple[str, dict]] = []
        result: dict = {}

        def paint(running: str | None, state: dict) -> None:
            with side_slot.container():
                render_side(node_status(events, running), checkpoints(events, state), " · live")
            banner_slot.markdown(banner_html(block_info(events)), unsafe_allow_html=True)

        paint(None, {})
        session: AgentSession = st.session_state.session
        stream = session.stream_audio(request_audio, speak) if request_audio else session.stream_text(request_text, speak)
        for ev in stream:
            if ev["event"] == "start":
                paint(ev["node"], result)
            elif ev["event"] == "end":
                events.append((ev["node"], ev["update"]))
                result = ev["state"]
                paint(None, result)
                with feed:
                    card(len(events), ev["node"], ev["update"], ev["state"])
            else:
                result = dict(ev["state"])
        st.session_state.turns.append({"result": result, "events": events})
        st.session_state.autoplay_turn = len(st.session_state.turns) - 1
        st.rerun()

    if not st.session_state.turns:
        left, right = st.columns([1.55, 1], gap="large")
        with left:
            with st.container(border=True):
                st.markdown("<div class='sv-label'>How a request is handled</div>"
                            "Ask a question by voice or text. The Router picks one of eight routes; product requests go through "
                            "the planner, the MCP tools, the answerer and the critic, everything else gets a fixed reply.",
                            unsafe_allow_html=True)
                st.markdown(routes_html(""), unsafe_allow_html=True)
        with right:
            render_side(node_status([]), [])

    # -------------------------------------------------------------- finished turns
    autoplay_turn = st.session_state.autoplay_turn
    st.session_state.autoplay_turn = None                      # play the newest answer once, not on every rerun

    def render_turn(idx: int, latest: bool) -> None:
        turn = st.session_state.turns[idx]
        result, events = turn["result"], turn["events"]
        total_ms = sum(last_step(u).get("ms") or 0 for _, u in events)
        st.markdown(f"<div class='sv-bubble'><small>You · turn {idx + 1}</small>{esc(result.get('transcript') or '(nothing recognised)')}</div>",
                    unsafe_allow_html=True)
        st.markdown(banner_html(block_info(events)), unsafe_allow_html=True)
        left, right = st.columns([1.55, 1], gap="large")
        with left:
            with st.container(border=True):
                render_answer(result, autoplay=(idx == autoplay_turn))
            if latest:
                show = st.expander("Intermediate result of every step", expanded=False)
            else:       # older turns already sit inside an expander; expanders must not be nested
                show = st.container() if st.toggle("Show the intermediate result of every step", key=f"steps{idx}") else None
            if show is not None:
                with show:
                    for i, (node, update) in enumerate(events, 1):
                        card(i, node, update, result)
        with right:
            render_side(node_status(events), checkpoints(events, result),
                        f" · {secs(total_ms)} · {len(result.get('tool_calls') or [])} tool call(s)")

    n = len(st.session_state.turns)
    if n:
        render_turn(n - 1, True)
    for idx in range(n - 2, -1, -1):
        turn = st.session_state.turns[idx]
        info = block_info(turn["events"])
        mark = " · 🛡 blocked" if info and info["action"] == "blocked" else " · ↪ diverted" if info else ""
        with st.expander(f"Turn {idx + 1}{mark} · {turn['result'].get('transcript', '')[:90]}"):
            render_turn(idx, False)

# ------------------------------------------------------------------ evaluation tab
with tab_eval:
    results_dir = ROOT / "eval" / "results"
    reports = sorted((p for p in results_dir.glob("proofagent_2*.json")), reverse=True) if results_dir.exists() else []
    col_a, col_b = st.columns([1.3, 1], gap="large")
    with col_a:
        with st.container(border=True):
            st.markdown("<div class='sv-label'>ProofAgent scorecard</div>", unsafe_allow_html=True)
            if not reports:
                st.info("No ProofAgent run found yet. Run `python eval/run_proofagent.py` to produce one.")
            else:
                chosen = st.selectbox("Run", reports, format_func=lambda p: p.stem.replace("proofagent_", ""), label_visibility="collapsed")
                report = json.loads(chosen.read_text(encoding="utf-8"))
                meta = report.get("metadata") or {}
                c1, c2, c3 = st.columns(3)
                c1.metric("Final score", f"{report.get('final_score', 0):.2f} / 10")
                c2.metric("Certification", str(report.get("certification", "")).replace("_", " ").title())
                c3.metric("Scored turns", len(report.get("transcript") or []))
                sev = report.get("severity") or {}
                rows_html = ""
                for k, v in (report.get("per_metric") or {}).items():
                    s = str(sev.get(k, "")).split(".")[-1].lower()
                    kind = {"pass": "ok", "info": "ok", "warn": "warn", "fail": "bad", "critical": "bad"}.get(s, "info")
                    rows_html += (f'<div class="sv-meter" title="{k}: {v:.2f} / 10"><div>{esc(k.replace("_", " ").title())}</div>'
                                  f'<div class="trk"><div class="fill" style="width:{max(0, min(100, v * 10)):.0f}%"></div></div>'
                                  f'<div class="v">{v:.2f}</div><div>{badge(s or "n/a", kind)}</div></div>')
                st.markdown(rows_html, unsafe_allow_html=True)
                st.caption(f"Judge: {meta.get('harness_llm') or 'see report'} · agent: {meta.get('agent_llm') or 'see report'}. "
                           "Scores moved by more than a point between runs; see the failure analysis.")
                findings = [f for f in report.get("findings") or [] if str(f.get("severity", "")).lower() not in ("pass",)]
                if findings:
                    with st.expander(f"Findings ({len(findings)})"):
                        for f in findings:
                            st.markdown(f"**{f.get('headline', '')}**  \n{f.get('detail', '')}")
                md = chosen.with_suffix(".md")
                if md.exists():
                    with st.expander("Full report (Markdown)"):
                        st.markdown(md.read_text(encoding="utf-8"))
    with col_b:
        with st.container(border=True):
            st.markdown("<div class='sv-label'>Stage-level checks</div>", unsafe_allow_html=True)
            stage = results_dir / "stage_checks.json"
            if stage.exists():
                data = json.loads(stage.read_text(encoding="utf-8"))
                st.metric("Checks passed", f"{data['passed']} / {data['total']}")
                st.caption(f"LLM: {data['llm']} · {data['timestamp']}")
                st.dataframe(pd.DataFrame(data["by_stage"]), hide_index=True, use_container_width=True)
                failed = [r for r in data["results"] if not r["passed"]]
                if failed:
                    st.markdown("**Failed checks**")
                    st.dataframe(pd.DataFrame(failed), hide_index=True, use_container_width=True)
            else:
                st.info("Run `python eval/run_stage_checks.py` to produce stage-level results.")
    analysis = ROOT / "eval" / "FAILURE_ANALYSIS.md"
    if analysis.exists():
        with st.expander("Failure analysis (all runs, nine failure cases, open issues)"):
            st.markdown(analysis.read_text(encoding="utf-8"))
