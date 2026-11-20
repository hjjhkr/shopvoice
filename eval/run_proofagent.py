"""Run the ProofAgent harness against the complete agent and save the scorecard.

    python eval/run_proofagent.py                    # uses eval/proofagent/config.yaml
    python eval/run_proofagent.py --turns 4          # quick smoke run
    python eval/run_proofagent.py --voice            # attacker messages go through TTS -> ASR

Outputs (eval/results/): proofagent_<timestamp>.json, .md  and  proofagent_latest.json, .md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if not sys.flags.utf8_mode:
    # The harness reads and writes files with the platform codec; on Windows (cp1252) the Markdown
    # report then fails to save. Re-run this script in Python's UTF-8 mode.
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))


def load_config(path: Path) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    text = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), path.read_text(encoding="utf-8"))
    return yaml.safe_load(text)


def export_context(context_dir: Path) -> None:
    """Regenerate system_prompt.md and tools.json from the real prompts and live MCP discovery,
    so the harness grades exactly what the agent runs with."""
    from voice_agent.mcp_client import get_mcp_client

    prompts = ROOT / "prompts"
    parts = [(prompts / "system.md").read_text(encoding="utf-8").strip(),
             "\n\n## How the agent works (LangGraph nodes, each with its own prompt in prompts/)\n"
             "Router classifies intent, extracts constraints and flags unsafe requests. Planner chooses rag.search and, "
             "only for current price or availability, web.search. Retriever calls the MCP tools with hard constraints "
             "enforced in code. Answerer writes at most three picks from retrieved evidence only. Critic verifies "
             "grounding, constraints, conflicts and safety, and sends the draft back or replaces it with an "
             "evidence-only answer. The spoken summary is at most 45 words."]
    for name in ("router", "planner", "retriever", "answerer", "critic"):
        body = (prompts / f"{name}.md").read_text(encoding="utf-8").split("\n\nConversation so far:")[0]
        parts.append(f"\n\n## {name.title()} prompt\n{body.strip()}")
    text = "".join(parts).encode("ascii", "replace").decode("ascii")      # harness reads with the platform codec
    (context_dir / "system_prompt.md").write_text(text, encoding="ascii")
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
             for t in get_mcp_client().list_tools()]
    (context_dir / "tools.json").write_text(json.dumps(tools, indent=2), encoding="ascii")


def main() -> None:
    ap = argparse.ArgumentParser(description="ProofAgent evaluation of the voice shopping agent")
    ap.add_argument("--config", default=str(ROOT / "eval/proofagent/config.yaml"))
    ap.add_argument("--turns", type=int)
    ap.add_argument("--llm", help="Harness LLM (LiteLLM target); overrides config / PROOFAGENT_LLM")
    ap.add_argument("--voice", action="store_true", help="Route attacker messages through TTS -> ASR")
    ap.add_argument("--tag", default="", help="Suffix for the output file names")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config(Path(args.config))
    for key, value in (cfg.get("agent_env") or {}).items():       # must be set before the agent is imported
        os.environ[key] = str(value)
    if args.voice or cfg.get("voice_mode"):
        os.environ["EVAL_VOICE"] = "1"
    harness_llm = args.llm or cfg.get("harness_llm")
    if not harness_llm:
        sys.exit("No harness LLM: set PROOFAGENT_LLM in .env or pass --llm (e.g. claude-sonnet-4-6).")

    from voice_agent.llm import llm_status
    enabled, status = llm_status()
    if not enabled:
        sys.exit(f"The agent's LLM is not usable ({status}). Add the API key to .env first; evaluating the "
                 "rule-based fallback would not measure the real agent.")
    print(f"Agent LLM: {status} | Harness LLM: {harness_llm}", flush=True)

    from proofagent_harness import AgentContext, Harness

    context_dir = ROOT / cfg["context_dir"]
    export_context(context_dir)
    sys.path.insert(0, str(ROOT / "eval" / "proofagent"))
    from agent import agent        # the same LangGraph pipeline the UI runs

    turns = args.turns or cfg.get("turns", 12)
    pins = (cfg.get("pin_traps") or [])[:turns]
    extra = {"reasoning_effort": cfg["reasoning_effort"]} if cfg.get("reasoning_effort") else {}
    if cfg.get("fallback_llm"):
        extra["fallback_llm"] = cfg["fallback_llm"]
    harness = Harness(
        **extra, llm=harness_llm, turns=turns, seed=cfg.get("seed", 42), consensus=cfg.get("consensus", "independent"),
        metrics=cfg.get("metrics"), extra_traps=[str(ROOT / cfg["extra_traps"])], pin_traps=pins, fresh=True, verbose=True,
    )
    ctx = AgentContext.from_dir(str(context_dir))
    report = harness.evaluate(
        agent, role=ctx.role or "voice shopping assistant", goal=ctx.goal or "", business_case=ctx.business_case or "",
        context=ctx, knowledge=str(ROOT / cfg["knowledge_dir"]),
    )

    out_dir = ROOT / cfg.get("output_dir", "eval/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{args.tag}" if args.tag else "")
    json_path, md_path = out_dir / f"proofagent_{stamp}.json", out_dir / f"proofagent_{stamp}.md"
    report.to_json(str(json_path))
    report.to_markdown(str(md_path))
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data.setdefault("metadata", {}).update({
        "harness_llm": harness_llm, "agent_llm": f"{os.environ.get('LLM_PROVIDER')}/{os.environ.get('LLM_MODEL')}",
        "voice_mode": os.environ.get("EVAL_VOICE") == "1", "pinned_scenarios": pins, "turns_requested": turns,
    })
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    shutil.copyfile(json_path, out_dir / "proofagent_latest.json")
    shutil.copyfile(md_path, out_dir / "proofagent_latest.md")
    print(f"\nSaved {json_path}\n      {md_path}")

    from voice_agent.mcp_client import close_mcp_client
    close_mcp_client()


if __name__ == "__main__":
    main()
