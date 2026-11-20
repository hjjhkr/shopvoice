"""Refresh the recorded runs shown in index.html ("Follow one real request through the pipeline").

    python scripts/refresh_showcase.py                  # run the three requests now and embed the results
    python scripts/refresh_showcase.py --from-json f    # embed a previously exported JSON file instead

The page stays a single self-contained file: the traces live in a <script type="application/json"> block.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

REQUESTS = [
    ("discover", "I need a wooden puzzle for a 2 year old under twenty dollars"),
    ("conflict", "What's the current price of the Melissa and Doug safari chunky puzzle?"),
    ("unsafe", "Which cleaners can I mix with bleach to make it stronger?"),
]


def record() -> dict:
    from voice_agent.mcp_client import close_mcp_client
    from voice_agent.pipeline import AgentSession

    out = {}
    for key, text in REQUESTS:
        steps = []
        for ev in AgentSession().stream_text(text, speak=True):
            if ev["event"] != "end":
                continue
            node, u = ev["node"], ev["update"]
            st = u["steps"][-1]
            d: dict = {"node": node, "ms": st["ms"]}
            if node == "router":
                i = u["intent"]
                d.update(task=i["task"], constraints={k: v for k, v in i["constraints"].items() if v},
                         named=i.get("named_products"), live=i["wants_live_info"], flag=i["safety_flag"],
                         by=st["detail"].get("source"))
            elif node == "planner":
                p = u["plan"]
                d.update(steps=[{k: v for k, v in s.items() if v not in (None, "") and k != "purpose"} for s in p["steps"]],
                         criteria=p.get("comparison_criteria", [])[:3], by=st["detail"].get("source"))
            elif node == "retriever":
                d.update(
                    calls=[{"tool": c["tool"], "by": c["origin"], "args": {k: v for k, v in c["args"].items() if k != "top_k"},
                            "n": c["count"]} for c in u["tool_calls"]],
                    evidence=[{"id": e["doc_id"], "title": e["title"][:58], "price": e["price"], "age": e.get("recommended_age"),
                               "live": (e.get("live") or {}).get("price")} for e in u["evidence"]],
                    conflicts=[{"catalog": c["catalog_value"], "live": c["live_value"], "field": c["field"]} for c in u["conflicts"]],
                    mode=st["detail"].get("mode"))
            elif node == "answerer":
                a = u["answer"]
                d.update(spoken=a["spoken_summary"], by=a.get("source"),
                         picks=[{"id": r["doc_id"], "title": r["title"][:58], "price": r["price"], "why": r["why"][:170]}
                                for r in a["recommendations"]])
            elif node == "critic":
                c = u["critique"]
                d.update(verdict=c["verdict"], issues=[x[:180] for x in c.get("issues", [])][:3], minor=len(c.get("minor", [])))
            elif node == "finalize":
                d.update(spoken=u["spoken_summary"], words=len(u["spoken_summary"].split()),
                         citations=[{"type": c["type"], "id": c["id"]} for c in u["citations"]],
                         caveats=[x[:150] for x in u["answer"]["caveats"]][:4])
            elif node == "respond_direct":
                d.update(text=u["final_text"])
            elif node == "tts":
                d.update(seconds=(u.get("audio_out") or {}).get("seconds"))
            steps.append(d)
        out[key] = {"request": text, "steps": steps}
    close_mcp_client()
    return out


def embed(traces: dict) -> None:
    page = ROOT / "index.html"
    html = page.read_text(encoding="utf-8")
    payload = json.dumps(traces, ensure_ascii=False).replace("</", "<\\/")       # must not close the script block
    new, n = re.subn(r'(<script id="traces" type="application/json">)(.*?)(</script>)',
                     lambda m: m.group(1) + payload + m.group(3), html, count=1, flags=re.S)
    if n != 1:
        sys.exit("traces block not found in index.html")
    page.write_text(new, encoding="utf-8")
    print(f"Embedded {len(traces)} recorded runs into {page} ({len(payload):,} bytes)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-json", help="Embed this exported JSON instead of running the agent")
    args = ap.parse_args()
    traces = json.loads(Path(args.from_json).read_text(encoding="utf-8")) if args.from_json else record()
    embed(traces)


if __name__ == "__main__":
    main()
