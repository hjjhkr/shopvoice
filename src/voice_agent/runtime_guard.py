"""ProofAgent Nano during the run, over MCP: every product-tool call and every tool result is checked.

ProofAgent's recommended production layout (agent-guardrails-cookbook, docs/mcp-vs-api.md, "hybrid") is used:

    user -> [input: POST /v1/scan]  -> Router -> Planner -> Retriever ----------------> Answerer -> Critic -> Finalize
                                                               |  rag.search / web.search (our MCP server)        |
                                                               |  check_tool_call  (ProofAgent MCP) before each    |
                                                               |  check_tool_result (ProofAgent MCP) after each    |
                                                                                    [output: POST /v1/scan] <-----+ -> TTS

The checks are MCP tools on ProofAgent's own server (https://nano.proofagent.ai/mcp: check_tool_call,
check_tool_result, ...). The agent's CODE calls them, not the model, so a prompt injection cannot skip them.

Decisions (the MCP tools answer ALLOW, ALLOW_MASKED, NEEDS_REVIEW or BLOCK):
  tool call    ALLOW / ALLOW_MASKED -> the call runs; NEEDS_REVIEW / BLOCK -> it does not run
  tool result  ALLOW -> results used as returned
               ALLOW_MASKED -> the masked text is used. Placeholders in url / doc_id / sku fields are put back: a
                               shop's product page is a public citation, not personal data, and Nano masks URLs.
               NEEDS_REVIEW / BLOCK -> one injected result blocks the whole batch, so each result is then checked on
                               its own and only the clean ones are kept (extra calls only when something was blocked)
A check that cannot be made follows PROOFAGENT_GUARD_ON_ERROR: block (default, the call or result is dropped) or
fallback (continue with ShopVoice's own sanitiser only).
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from contextlib import AsyncExitStack
from typing import Any

from .config import get_settings
from .logging_utils import log_event

STOP = {"NEEDS_REVIEW", "BLOCK"}
# Never masked: identifiers and citations, and the product's own name and brand, which are public product data that
# Nano tends to read as personal names ("Mrs. Meyer's", "Bar Keepers Friend"). Descriptions and snippets stay masked.
PROTECTED = ("url", "doc_id", "sku", "source_domain", "source", "title", "brand")
_PLACEHOLDER = re.compile(r"\[[A-Z_]+:[A-Z_]+#\d+\]")
_last_usage: dict[str, Any] = {}


def checkpoints() -> set[str]:
    """Which ProofAgent checkpoints are on: input, tool_call, tool_result, output (PROOFAGENT_GUARD_CHECKPOINTS)."""
    s = get_settings()
    if s.guard_mode != "on":
        return set()
    return {c.strip() for c in s.guard_checkpoints.split(",") if c.strip()}


def last_usage() -> dict[str, Any]:
    """The free-call counter ProofAgent returned with the most recent check (for the UI)."""
    return dict(_last_usage)


class NanoMCP:
    """A persistent MCP session with ProofAgent Nano's MCP server (streamable HTTP), on a background loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._session = None
        self._stop: asyncio.Event | None = None
        self.server = ""
        self.tools: list[str] = []
        self._thread = threading.Thread(target=self._run, name="proofagent-mcp", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise TimeoutError("ProofAgent MCP server did not answer within 30 s")
        if self._error:
            raise RuntimeError(f"Could not open the ProofAgent MCP session: {type(self._error).__name__}") from self._error

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except BaseException as e:  # noqa: BLE001 - reported to the caller thread
            self._error = e
            self._ready.set()

    async def _main(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        s = get_settings()
        self._stop = asyncio.Event()
        async with AsyncExitStack() as stack:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(f"{s.nano_url}/mcp", headers={"Authorization": f"Bearer {s.nano_key}"},
                                      timeout=s.guard_timeout))
            self._session = await stack.enter_async_context(ClientSession(read, write))
            info = await self._session.initialize()
            self.server = f"{info.serverInfo.name} {info.serverInfo.version}"
            self.tools = [t.name for t in (await self._session.list_tools()).tools]      # MCP discovery
            self._ready.set()
            await self._stop.wait()

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        s = get_settings()
        res = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, arguments), self._loop) \
            .result(timeout=s.guard_timeout + 5)
        if res.isError:
            raise RuntimeError("".join(getattr(c, "text", "") for c in res.content)[:200] or "MCP tool error")
        return res.structuredContent or {}

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)


_nano: NanoMCP | None = None
_lock = threading.Lock()


def get_nano_mcp() -> NanoMCP:
    global _nano
    with _lock:
        if _nano is None:
            _nano = NanoMCP()
        return _nano


def close_nano_mcp() -> None:
    global _nano
    with _lock:
        if _nano is not None:
            _nano.close()
            _nano = None


def check(tool: str, arguments: dict[str, Any], checkpoint: str, nano: Any = None) -> dict[str, Any]:
    """One ProofAgent MCP check. Never raises: a failed check is reported with action ERROR and a `decision`
    that follows PROOFAGENT_GUARD_ON_ERROR."""
    s = get_settings()
    t0 = time.perf_counter()
    try:
        out = (nano or get_nano_mcp()).call(tool, arguments)
        action = str(out.get("action") or "BLOCK").upper()
        if out.get("usage"):
            _last_usage.update(out["usage"])
        rec = {"checkpoint": checkpoint, "mcp_tool": tool, "action": action,
               "decision": "stop" if action in STOP else ("mask" if action == "ALLOW_MASKED" else "allow"),
               "reason": str(out.get("reason") or "")[:300], "use_this": out.get("use_this"),
               "request_id": ((out.get("result") or {}).get("request") or {}).get("id"),
               "proofagent_decision": out.get("proofagent_decision")}
    except Exception as e:  # noqa: BLE001 - network, auth, quota, protocol
        rec = {"checkpoint": checkpoint, "mcp_tool": tool, "action": "ERROR",
               "decision": "allow" if s.guard_on_error == "fallback" else "stop",
               "reason": f"ProofAgent MCP check failed ({type(e).__name__}); "
                         + ("continuing with ShopVoice's own checks" if s.guard_on_error == "fallback" else "failing closed"),
               "use_this": None, "request_id": None, "proofagent_decision": None}
    rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    log_event("proofagent_guard", {k: v for k, v in rec.items() if k != "use_this"})
    return rec


def _restore(original: Any, masked: Any) -> Any:
    """Masked copy of a result, with identifiers and citation fields taken from the original."""
    if isinstance(original, dict) and isinstance(masked, dict):
        return {k: (original[k] if k in PROTECTED or not isinstance(original[k], (str, dict, list))
                    else _restore(original[k], masked.get(k, original[k]))) for k in original}
    if isinstance(original, list) and isinstance(masked, list) and len(original) == len(masked):
        return [_restore(o, m) for o, m in zip(original, masked)]
    if isinstance(original, str) and isinstance(masked, str):
        return masked
    return original


def _apply_mask(results: list[dict], use_this: Any) -> list[dict] | None:
    try:
        data = json.loads(use_this) if isinstance(use_this, str) else use_this
        masked = data.get("results") if isinstance(data, dict) else data
        if not isinstance(masked, list) or len(masked) != len(results):
            return None
        return _restore(results, masked)
    except (TypeError, ValueError):
        return None


class GuardedToolClient:
    """Wraps the product-discovery MCP client so that every tool call and result goes through ProofAgent's MCP
    checks. `events` collects one record per check for the step log and the UI."""

    def __init__(self, client: Any, events: list[dict], active: set[str] | None = None, nano: Any = None):
        self.client, self.events, self.nano = client, events, nano
        self.active = checkpoints() if active is None else active

    def list_tools(self) -> list[dict[str, Any]]:
        return self.client.list_tools()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = {k: v for k, v in arguments.items() if v is not None}
        if "tool_call" in self.active:
            rec = check("check_tool_call", {"tool": name, "arguments": args}, "tool_call", self.nano)
            rec["tool"] = name
            self.events.append(rec)
            if rec["decision"] == "stop":
                return {"results": [], "error": f"blocked by ProofAgent (check_tool_call: {rec['action']})",
                        "proofagent_blocked": True}
        out = self.client.call_tool(name, arguments)
        results = out.get("results") or []
        if "tool_result" in self.active and results:
            out = dict(out, results=self._check_results(name, results))
        return out

    def _check_results(self, name: str, results: list[dict]) -> list[dict]:
        """Check the text of the results that the model will read. Nano reads about 6,000 characters of input and cuts
        the rest, so the text is packed into chunks below MAX_CHUNK_CHARS and every chunk is checked."""
        views = [_model_view(r) for r in results]
        chunks, cur = [], []
        for i, v in enumerate(views):
            if cur and len(json.dumps({"results": [views[j] for j in cur + [i]]})) > MAX_CHUNK_CHARS:
                chunks.append(cur)
                cur = []
            cur.append(i)
        if cur:
            chunks.append(cur)
        rank = {"ALLOW": 0, "ALLOW_MASKED": 1, "NEEDS_REVIEW": 2, "BLOCK": 3, "ERROR": 4}
        summary = {"checkpoint": "tool_result", "mcp_tool": "check_tool_result", "tool": name, "action": "ALLOW",
                   "decision": "allow", "reasons": [], "results_in": len(results), "chunks": len(chunks),
                   "per_item_checks": 0, "latency_ms": 0, "request_id": None}
        kept: dict[int, dict] = {}
        for idx in chunks:
            rec = check("check_tool_result", {"tool": name, "result": json.dumps({"results": [views[i] for i in idx]})},
                        "tool_result", self.nano)
            summary["latency_ms"] += rec["latency_ms"]
            summary["request_id"] = summary["request_id"] or rec["request_id"]
            if rank.get(rec["action"], 3) > rank.get(summary["action"], 0):
                summary.update(action=rec["action"], decision=rec["decision"])
            if rec["reason"] and rec["reason"] not in summary["reasons"]:
                summary["reasons"].append(rec["reason"])
            if rec["decision"] == "allow":
                kept.update({i: results[i] for i in idx})
            elif rec["decision"] == "mask":
                masked = _apply_mask([results[i] for i in idx], rec.get("use_this"))
                if masked is None:      # a mask is not a block: keep the result, say the mask could not be applied
                    summary["reasons"].append("masked text could not be mapped back; ShopVoice's own sanitiser applies")
                    masked = [results[i] for i in idx]
                kept.update(dict(zip(idx, masked)))
            elif rec["action"] == "ERROR" or len(idx) == 1:
                summary.setdefault("withheld", []).extend(_label(results[i]) for i in idx)
            else:
                # One bad result stops its whole chunk: check each result on its own and keep the clean ones.
                for i in idx:
                    one = check("check_tool_result", {"tool": name, "result": json.dumps({"results": [views[i]]})},
                                "tool_result", self.nano)
                    summary["per_item_checks"] += 1
                    summary["latency_ms"] += one["latency_ms"]
                    if one["decision"] == "allow":
                        kept[i] = results[i]
                    elif one["decision"] == "mask":
                        kept[i] = (_apply_mask([results[i]], one.get("use_this")) or [results[i]])[0]
                    else:
                        summary.setdefault("withheld", []).append(_label(results[i]))
        out = [kept[i] for i in sorted(kept)]
        summary["results_out"] = len(out)
        summary["reason"] = " ".join(summary.pop("reasons"))[:400]
        self.events.append(summary)
        return out


MAX_CHUNK_CHARS = 5000
_VIEW_FIELDS = ("doc_id", "url", "title", "brand", "features", "claims", "ingredients", "snippet")


def _model_view(r: dict) -> dict:
    """The fields of a result that reach the model as text (identifiers kept so masks map back by position)."""
    v = {k: r[k] for k in _VIEW_FIELDS if isinstance(r.get(k), str) and r[k]}
    if "features" in v:
        v["features"] = v["features"][:500]
    return v


def _label(r: dict) -> str:
    return r.get("url") or r.get("doc_id") or (r.get("title") or "")[:60]
