"""LangGraph wiring.

    START -> asr -> input_guard -+-> router -+-> planner -> retriever -> answerer -> critic -+-> finalize -+
                                 |           |                  |          ^----- revise ---+             |
                                 |           |                  +- every rag.search / web.search call is   +-> output_guard -> tts -> END
                                 |           |                     checked over ProofAgent's MCP server    |
                                 |           +-> respond_direct (refuse / clarify / small talk) ----------+
                                 +-> respond_direct (ProofAgent block / review, or failed scan) ----------+

input_guard and output_guard call ProofAgent Nano's API (POST /v1/scan); during the run the Retriever's tool calls and
tool results are checked with ProofAgent's MCP tools (check_tool_call, check_tool_result). See runtime_guard.py.
"""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from . import nodes as n


@lru_cache(maxsize=1)
def build_graph():
    g = StateGraph(n.AgentState)
    g.add_node("asr", n.asr_node)
    g.add_node("input_guard", n.input_guard_node)
    g.add_node("router", n.router_node)
    g.add_node("planner", n.planner_node)
    g.add_node("retriever", n.retriever_node)
    g.add_node("answerer", n.answerer_node)
    g.add_node("critic", n.critic_node)
    g.add_node("finalize", n.finalize_node)
    g.add_node("respond_direct", n.respond_direct_node)
    g.add_node("output_guard", n.output_guard_node)
    g.add_node("tts", n.tts_node)

    g.add_edge(START, "asr")
    g.add_edge("asr", "input_guard")
    g.add_conditional_edges("input_guard", n.route_after_input_guard, {"router": "router", "respond_direct": "respond_direct"})
    g.add_conditional_edges("router", n.route_after_router, {"planner": "planner", "respond_direct": "respond_direct"})
    g.add_edge("planner", "retriever")
    g.add_edge("retriever", "answerer")
    g.add_edge("answerer", "critic")
    g.add_conditional_edges("critic", n.route_after_critic, {"answerer": "answerer", "finalize": "finalize"})
    g.add_edge("finalize", "output_guard")
    g.add_edge("respond_direct", "output_guard")
    g.add_edge("output_guard", "tts")
    g.add_edge("tts", END)
    return g.compile()


def mermaid() -> str:
    return build_graph().get_graph().draw_mermaid()
