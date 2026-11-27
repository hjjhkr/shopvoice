"""Stage-level checks: run scenarios through the real pipeline and attribute each
pass/fail to ASR, Router, Planner, RAG/MCP, Answerer/Critic or TTS.

    python eval/run_stage_checks.py                 # text path, LLM from .env
    python eval/run_stage_checks.py --voice         # TTS -> ASR -> agent -> TTS for every scenario
    LLM_PROVIDER=rules python eval/run_stage_checks.py    # deterministic baseline (no model)

Writes eval/results/stage_checks[_voice].json and prints a per-stage table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("WEB_SEARCH_PROVIDER", "fixture")      # reproducible live results


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate after normalising case, punctuation and '$20' vs 'twenty dollars'."""
    from voice_agent.graph.rules import spoken_numbers_to_digits

    def norm(t: str) -> list[str]:
        t = spoken_numbers_to_digits(t.lower()).replace("'", "").replace("’", "")
        t = re.sub(r"\$\s?(\d+)", r"\1 dollars", t)
        t = re.sub(r"(\d)\s*gb\b", r"\1 gigabytes", t)            # "16GB" == "sixteen gigabytes"
        t = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", t)
        return re.sub(r"[^a-z0-9 ]", " ", t).split()
    r, h = norm(reference), norm(hypothesis)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / max(1, len(r))


def check(sc: dict, result: dict, voice: bool, said: str) -> list[dict]:
    exp = sc["expect"]
    intent = result.get("intent") or {}
    c = intent.get("constraints") or {}
    plan = result.get("plan") or {}
    calls = result.get("tool_calls") or []
    recs = (result.get("answer") or {}).get("recommendations") or []
    text = result.get("final_text", "")
    spoken = result.get("spoken_summary", "")
    planned = [s["tool"] for s in plan.get("steps", [])]
    called = [x["tool"] for x in calls]
    out: list[dict] = []

    def add(stage: str, name: str, ok: bool, detail: str = "") -> None:
        out.append({"scenario": sc["id"], "category": sc["category"], "stage": stage, "check": name,
                    "passed": bool(ok), "detail": detail})

    if voice:
        rate = wer(said, result.get("transcript", ""))
        add("ASR", "word error rate <= 0.15", rate <= 0.15, f"WER={rate:.2f}; heard: {result.get('transcript', '')!r}")
    if "task" in exp:
        add("Router", f"task == {exp['task']}", intent.get("task") == exp["task"], f"got {intent.get('task')}")
    if "task_in" in exp:
        add("Router", f"task in {exp['task_in']}", intent.get("task") in exp["task_in"], f"got {intent.get('task')}")
    for key in ("max_price", "min_price"):
        if key in exp:
            add("Router", f"{key} == {exp[key]}", c.get(key) is not None and abs(c[key] - exp[key]) < 0.01, f"got {c.get(key)}")
    if "brand" in exp:
        add("Router", f"brand ~ {exp['brand']}", exp["brand"] in (c.get("brand") or "").lower(), f"got {c.get('brand')}")
    if "min_rating" in exp:
        add("Router", f"min_rating == {exp['min_rating']}", c.get("min_rating") is not None and abs(c["min_rating"] - exp["min_rating"]) < 0.01,
            f"got {c.get('min_rating')}")
    if "preference" in exp:
        add("Router", f"preference '{exp['preference']}' kept", any(exp["preference"] in p.lower() for p in c.get("preferences") or []),
            f"got {c.get('preferences')}")
    if exp.get("no_material"):
        add("Router", "surface is not taken as a material", not c.get("material"), f"got material={c.get('material')!r}")
    if "wants_live" in exp:
        add("Router", f"wants_live == {exp['wants_live']}", intent.get("wants_live_info") == exp["wants_live"],
            f"got {intent.get('wants_live_info')}")
    for tool in exp.get("tools", []):
        add("Planner", f"plans {tool}", tool in planned, f"planned {planned}")
        add("RAG/MCP", f"calls {tool}", tool in called, f"called {called}")
    for tool in exp.get("no_tools", []):
        add("Planner", f"does not call {tool}", tool not in called, f"called {called}")
    if "all_under" in exp:
        over = [e["doc_id"] for e in result.get("evidence") or [] if e["price"] > exp["all_under"]]
        add("RAG/MCP", f"all evidence <= ${exp['all_under']}", not over, f"over budget: {over}")
        add("Answerer", f"all picks <= ${exp['all_under']}", all(r["price"] <= exp["all_under"] for r in recs),
            str([r["price"] for r in recs]))
    if "all_rating_at_least" in exp:
        floor = exp["all_rating_at_least"]
        rag_args = [x.get("args") or {} for x in calls if x["tool"] == "rag.search"]
        add("Planner", f"rag.search filters min_rating >= {floor}", any((a.get("min_rating") or 0) >= floor for a in rag_args),
            str([a.get("min_rating") for a in rag_args]))
        add("Answerer", f"all picks rated >= {floor}", bool(recs) and all((r.get("rating") or 0) >= floor for r in recs),
            str([r.get("rating") for r in recs]))
    if "all_over" in exp:
        add("Answerer", f"all picks >= ${exp['all_over']}", all(r["price"] >= exp["all_over"] for r in recs),
            str([r["price"] for r in recs]))
    if "conflict" in exp:
        add("RAG/MCP", "catalog/live conflict detected", bool(result.get("conflicts")) == exp["conflict"],
            f"{len(result.get('conflicts') or [])} conflict(s)")
    if "titles_contain" in exp:
        add("RAG/MCP", f"picks are '{exp['titles_contain']}' products",
            bool(recs) and all(exp["titles_contain"] in r["title"].lower() for r in recs), str([r["title"][:40] for r in recs]))
    if "min_recs" in exp:
        add("Answerer", f">= {exp['min_recs']} recommendation(s)", len(recs) >= exp["min_recs"], f"got {len(recs)}")
    if "max_recs" in exp:
        add("Answerer", f"<= {exp['max_recs']} recommendation(s)", len(recs) <= exp["max_recs"], f"got {len(recs)}")
    if exp.get("cited"):
        ev_ids = {e["doc_id"] for e in result.get("evidence") or []}
        add("Answerer", "every pick cites a retrieved doc_id", bool(recs) and all(r["doc_id"] in ev_ids and r["doc_id"] in text for r in recs))
    if exp.get("live_cited"):
        add("Answerer", "live source URL cited", any(ci["type"] == "live" for ci in result.get("citations") or []))
    for needle in exp.get("text_contains", []):
        options = needle if isinstance(needle, list) else [needle]       # a list = any of these wordings
        add("Answerer", f"mentions '{options[0]}'" + (" (or an equivalent wording)" if len(options) > 1 else ""),
            any(o.lower() in text.lower() for o in options), text[:120].replace("\n", " "))
    for needle in exp.get("text_not_contains", []):
        add("Answerer", f"never mentions '{needle}'", needle.lower() not in text.lower())
    critique = result.get("critique") or {}
    add("Answerer", "critic: final answer grounded", critique.get("grounded", True) is True, str(critique.get("verdict")))
    add("TTS", "spoken summary <= 45 words", len(spoken.split()) <= 45, f"{len(spoken.split())} words")
    if voice:
        audio = result.get("audio_out") or {}
        add("TTS", "audio produced, <= 15 s", bool(audio.get("path")) and (audio.get("seconds") or 99) <= 15,
            f"{audio.get('seconds')} s" if audio.get("path") else str(audio.get("error")))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--voice", action="store_true")
    ap.add_argument("--only", help="Run a single scenario id")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from voice_agent.config import get_settings
    from voice_agent.llm import llm_status
    from voice_agent.mcp_client import close_mcp_client
    from voice_agent.pipeline import AgentSession
    from voice_agent.speech.tts import synthesize

    settings = get_settings()
    scenarios = yaml.safe_load((ROOT / "eval/stage_scenarios.yaml").read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] == args.only]

    results: list[dict] = []
    for sc in scenarios:
        session, result = AgentSession(), {}
        for i, turn in enumerate(sc["turns"]):
            last = i == len(sc["turns"]) - 1
            if args.voice:
                clip = synthesize(turn)
                result = session.run_audio(clip["path"], speak=last)
                Path(clip["path"]).unlink(missing_ok=True)
            else:
                result = session.run_text(turn)
        rows = check(sc, result, args.voice, sc["turns"][-1])
        results += rows
        failed = [r for r in rows if not r["passed"]]
        print(f"{'PASS' if not failed else 'FAIL'}  {sc['id']:<28} {len(rows) - len(failed)}/{len(rows)}"
              + "".join(f"\n        x [{r['stage']}] {r['check']} ({r['detail']})" for r in failed), flush=True)
    close_mcp_client()

    stages = ["ASR", "Router", "Planner", "RAG/MCP", "Answerer", "TTS"]
    by_stage = []
    for st in stages:
        rows = [r for r in results if r["stage"] == st]
        if rows:
            ok = sum(r["passed"] for r in rows)
            by_stage.append({"stage": st, "passed": ok, "total": len(rows), "pass_rate": f"{100 * ok / len(rows):.0f}%"})
    passed = sum(r["passed"] for r in results)
    summary = {
        "timestamp": datetime.now().isoformat(timespec="seconds"), "voice": args.voice,
        "llm": llm_status()[1] if llm_status()[0] else "rules (no model)",
        "asr": f"{settings.asr_provider}/{settings.asr_model}", "tts": settings.tts_provider,
        "scenarios": len(scenarios), "scenarios_fully_passed": len({r["scenario"] for r in results}) - len({r["scenario"] for r in results if not r["passed"]}),
        "passed": passed, "total": len(results), "by_stage": by_stage, "results": results,
    }
    out = ROOT / "eval/results" / ("stage_checks_voice.json" if args.voice else "stage_checks.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n{passed}/{len(results)} checks passed; {summary['scenarios_fully_passed']}/{len(scenarios)} scenarios clean  (LLM {summary['llm']})")
    for row in by_stage:
        print(f"  {row['stage']:<9} {row['passed']:>3}/{row['total']:<3} {row['pass_rate']}")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
