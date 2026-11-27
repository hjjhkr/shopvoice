"""Public entry points. A session keeps short conversational memory so follow-ups
("a cheaper one", "what about under ten dollars") resolve against the previous turn.

    python -m voice_agent.pipeline "eco-friendly stainless steel cleaner under 15 dollars"
    python -m voice_agent.pipeline --audio question.wav --speak
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from typing import Any

from .graph.build import build_graph


class AgentSession:
    def __init__(self, max_history: int = 12):
        self.history: list[dict[str, Any]] = []
        self.max_history = max_history

    def _initial(self, state: dict[str, Any]) -> dict[str, Any]:
        return {**state, "history": list(self.history), "tool_calls": [], "steps": [], "revisions": 0}

    def stream(self, state: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Run one turn and yield progress events as each LangGraph node starts and finishes:
            {"event": "start", "node": name}
            {"event": "end",   "node": name, "update": what the node wrote, "state": state so far}
            {"event": "done",  "state": final state}
        Used by the UI to show where the request is and the intermediate results."""
        state = self._initial(state)
        acc = dict(state)
        for mode, chunk in build_graph().stream(state, {"recursion_limit": 25}, stream_mode=["tasks", "updates"]):
            if mode == "tasks":
                if "input" in chunk:                       # a task-start event (results carry "result" instead)
                    yield {"event": "start", "node": chunk["name"]}
                continue
            for node, update in chunk.items():
                for key, value in (update or {}).items():
                    acc[key] = acc.get(key, []) + value if key in ("steps", "tool_calls") else value
                yield {"event": "end", "node": node, "update": update or {}, "state": acc}
        self._remember(acc)
        yield {"event": "done", "state": acc}

    def stream_text(self, text: str, speak: bool = False) -> Iterator[dict[str, Any]]:
        return self.stream({"transcript": text, "synthesize": speak})

    def stream_audio(self, audio_path: str, speak: bool = True) -> Iterator[dict[str, Any]]:
        return self.stream({"audio_in": audio_path, "synthesize": speak})

    def _run(self, state: dict[str, Any]) -> dict[str, Any]:
        result = build_graph().invoke(self._initial(state), {"recursion_limit": 25})
        self._remember(result)
        return result

    def _remember(self, result: dict[str, Any]) -> None:
        recs = (result.get("answer") or {}).get("recommendations") or []
        self.history.append({
            "user": result.get("transcript", ""),
            "assistant": result.get("spoken_summary", ""),
            "intent": result.get("intent", {}),
            "shown": [{"doc_id": r["doc_id"], "title": r["title"], "price": r.get("price")} for r in recs],
        })
        self.history = self.history[-self.max_history:]

    def run_text(self, text: str, speak: bool = False) -> dict[str, Any]:
        return self._run({"transcript": text, "synthesize": speak})

    def run_audio(self, audio_path: str, speak: bool = True) -> dict[str, Any]:
        return self._run({"audio_in": audio_path, "synthesize": speak})

    def reset(self) -> None:
        self.history.clear()


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one turn of the voice shopping agent")
    ap.add_argument("text", nargs="?", help="User request as text")
    ap.add_argument("--audio", help="Path to a WAV/MP3 recording instead of text")
    ap.add_argument("--speak", action="store_true", help="Synthesize the spoken summary")
    ap.add_argument("--json", action="store_true", help="Print the full final state as JSON")
    args = ap.parse_args()
    if not args.text and not args.audio:
        ap.error("give a text request or --audio")

    sys.stdout.reconfigure(encoding="utf-8")
    session = AgentSession()
    result = session.run_audio(args.audio, args.speak) if args.audio else session.run_text(args.text, args.speak)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print("TRANSCRIPT:", result.get("transcript"))
    for s in result["steps"]:
        print(f"  [{s['node']:<14}] {s['ms']:>6} ms  {s['summary']}")
    print("\n" + result["final_text"])
    if result.get("audio_out"):
        print("\nAUDIO:", result["audio_out"])


if __name__ == "__main__":
    main()
