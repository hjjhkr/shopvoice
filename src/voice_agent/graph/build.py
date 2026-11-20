"""LangGraph wiring.

    START -> asr -> router -+-> planner -> retriever -> answerer -> critic -+-> finalize -> tts -> END
                            |                              ^----- revise ---+
                            +-> respond_direct (refuse / clarify / small talk) ---------> tts -> END
"""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from . import nodes as n


@lru_cache(maxsize=1)
def build_graph():
    g = StateGraph(n.AgentState)
    g.add_node("asr", n.asr_node)
    g.add_node("router", n.router_node)
    g.add_node("planner", n.planner_node)
    g.add_node("retriever", n.retriever_node)
    g.add_node("answerer", n.answerer_node)
    g.add_node("critic", n.critic_node)
    g.add_node("finalize", n.finalize_node)
    g.add_node("respond_direct", n.respond_direct_node)
    g.add_node("tts", n.tts_node)

    g.add_edge(START, "asr")
    g.add_edge("asr", "router")
    g.add_conditional_edges("router", n.route_after_router, {"planner": "planner", "respond_direct": "respond_direct"})
    g.add_edge("planner", "retriever")
    g.add_edge("retriever", "answerer")
    g.add_edge("answerer", "critic")
    g.add_conditional_edges("critic", n.route_after_critic, {"answerer": "answerer", "finalize": "finalize"})
    g.add_edge("finalize", "tts")
    g.add_edge("respond_direct", "tts")
    g.add_edge("tts", END)
    return g.compile()


def mermaid() -> str:
    return build_graph().get_graph().draw_mermaid()
