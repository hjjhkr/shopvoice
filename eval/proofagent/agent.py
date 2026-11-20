"""The agent under test, as ProofAgent sees it: one callable `agent(message) -> AgentResponse`.

It runs the SAME LangGraph pipeline as the UI (Router -> Planner -> MCP tools -> Answerer -> Critic),
keeps conversational memory across turns, and returns the tool calls and retrievals so the jury can
score tool use and grounding, not just the text.

    proof run eval/proofagent/agent.py --context-dir eval/proofagent/context ...
    python eval/run_proofagent.py            (preferred: applies eval/proofagent/config.yaml)

EVAL_VOICE=1 routes every message through TTS -> ASR first, so the evaluation covers the voice path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from proofagent_harness import AgentResponse  # noqa: E402

from voice_agent.pipeline import AgentSession  # noqa: E402

_session = AgentSession()
VOICE = os.environ.get("EVAL_VOICE", "0") == "1"


def _through_voice(message: str) -> str:
    """Speak the message with TTS and transcribe it back, as a real voice user would be heard."""
    from voice_agent.speech.asr import transcribe
    from voice_agent.speech.tts import synthesize

    audio = synthesize(message[:600])
    try:
        return transcribe(audio["path"])["text"] or message
    finally:
        Path(audio["path"]).unlink(missing_ok=True)


def agent(message: str) -> AgentResponse:
    heard = _through_voice(message) if VOICE else message
    result = _session.run_text(heard)
    tools_called = [
        {"name": c["tool"], "args": c["args"],
         # the jury needs what the tool returned, not just a count, to verify prices and availability
         "result": {"count": c["count"], "error": c.get("error"), "results": c.get("results") or c["ids"]}}
        for c in result.get("tool_calls", [])
    ]
    retrievals = [
        {"source": e["doc_id"],
         # every field the agent may quote must be visible to the jury, or a grounded claim looks invented
         "chunk": f"{e['title']} | catalog price ${e['price']} (Jan 2020) | rating {e.get('rating')} | "
                  f"brand {e.get('brand')} ({e.get('brand_source')}) | materials {e.get('ingredients')} | "
                  f"manufacturer recommended age {e.get('recommended_age')} | category {e.get('category')} | "
                  f"{(e.get('features') or '')[:300]}"
                  + (f" | note: {e['budget_note']}" if e.get("budget_note") else "")
                  + (f" | live: {e['live']}" if e.get("live") else ""),
         "score": e.get("similarity")}
        for e in result.get("evidence", [])
    ]
    critique = result.get("critique") or {}
    return AgentResponse(
        text=result.get("final_text", ""),
        tools_called=tools_called,
        retrievals=retrievals,
        memory_snapshot={"heard": heard, "intent": result.get("intent"), "plan": result.get("plan"),
                         "conflicts": result.get("conflicts"), "critic_verdict": critique.get("verdict")},
        reasoning=" | ".join(f"{s['node']}: {s['summary']}" for s in result.get("steps", [])),
    )
