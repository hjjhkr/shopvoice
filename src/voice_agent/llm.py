"""Model-agnostic LLM access.

LLM_PROVIDER / LLM_MODEL select any LangChain chat model (openai, anthropic,
google_genai, bedrock_converse, ollama, ...). LLM_PROVIDER=rules disables the LLM:
every node then uses its deterministic fallback, which is also what runs if a
model call fails. Every prompt sent to a model is written to logs/prompts.jsonl.
"""
from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import ROOT, get_settings
from .logging_utils import log_event

PROMPT_DIR = ROOT / "prompts"
T = TypeVar("T", bound=BaseModel)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def render(name: str, **variables: Any) -> str:
    text = load_prompt(name)
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


_KEY_VARS = {"anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "openai": ("OPENAI_API_KEY",),
             "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY")}


def llm_status() -> tuple[bool, str]:
    """(enabled, human-readable reason). A hosted provider without its API key is treated as disabled,
    so the pipeline runs on the rule-based fallbacks instead of failing every call."""
    s = get_settings()
    if s.llm_provider in ("", "rules", "none") or not s.llm_model:
        return False, "rule-based fallback (no LLM configured)"
    needed = _KEY_VARS.get(s.llm_provider)
    if needed and not any(os.environ.get(v, "").strip() for v in needed) and not s.llm_base_url:
        return False, f"{s.llm_provider}/{s.llm_model}: {needed[0]} is not set, using rule-based fallback"
    return True, f"{s.llm_provider}/{s.llm_model}"


def llm_enabled() -> bool:
    return llm_status()[0]


def _is_claude(provider: str, model: str) -> bool:
    return provider.startswith("anthropic") or "claude" in model.lower()


def structured_mode() -> str:
    """How a node gets a schema-shaped reply (both use tool/function calling):
      forced  the schema is the only tool and the model is forced to call it (OpenAI, Ollama, Gemini)
      auto    the schema is offered as a tool with tool_choice=auto and the prompt asks for the call;
              required for current Claude models, which reject a forced tool_choice
    Override with LLM_STRUCTURED_MODE=forced|auto."""
    explicit = os.environ.get("LLM_STRUCTURED_MODE", "").strip().lower()
    if explicit in ("forced", "auto"):
        return explicit
    s = get_settings()
    return "auto" if _is_claude(s.llm_provider, s.llm_model) else "forced"


@lru_cache(maxsize=4)
def _model(provider: str, model: str, temperature: float | None, base_url: str):
    from langchain.chat_models import init_chat_model

    # Every call is bounded: a model that loops inside a structured output must not hang the pipeline
    # (seen with a local model: 19,693 tokens and 13 minutes on one call). On a timeout or a truncated,
    # unparseable reply the node falls back to its deterministic path.
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "4000"))     # includes thinking tokens on Claude 5 models
    timeout = float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))
    kwargs: dict[str, Any] = {}
    if temperature is not None:                  # opt-in via LLM_TEMPERATURE; by default nothing is sent
        kwargs["temperature"] = temperature
    if base_url:
        kwargs["base_url"] = base_url
    if provider == "ollama":
        kwargs["reasoning"] = False      # no <think> blocks from qwen3 / deepseek-r1 style models
        kwargs["num_predict"] = max_tokens
        kwargs["client_kwargs"] = {"timeout": timeout}
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["timeout"] = timeout
    effort = os.environ.get("LLM_EFFORT", "low").strip().lower()
    if effort and _is_claude(provider, model) and provider == "anthropic":
        # Claude 5 models think before answering; a voice turn makes 5-7 calls, so the default is the
        # lowest effort (fastest, cheapest). Raise with LLM_EFFORT=medium|high if quality matters more.
        kwargs["output_config"] = {"effort": effort}
    seed = os.environ.get("LLM_SEED", "42").strip()
    if seed and provider in ("ollama", "openai"):      # providers that accept a sampling seed
        kwargs["seed"] = int(seed)                     # same input -> same output, so runs are repeatable
    return init_chat_model(model, model_provider=provider, **kwargs)


def get_chat_model():
    """The configured chat model, or None in rules mode."""
    if not llm_enabled():
        return None
    s = get_settings()
    return _model(s.llm_provider, s.llm_model, s.llm_temperature, s.llm_base_url)


def call_structured(node: str, schema: type[T], user_prompt: str, retries: int = 1) -> T | None:
    """Run one node prompt and parse the reply into `schema` via function calling.
    Returns None (caller falls back to rules) if the LLM is disabled or keeps failing."""
    model = get_chat_model()
    if model is None:
        return None
    from langchain_core.messages import HumanMessage, SystemMessage

    s = get_settings()
    system = load_prompt("system")
    mode = structured_mode()
    if mode == "auto":
        runnable = model.bind_tools([schema])        # tool_choice defaults to auto
        user_prompt = f"{user_prompt}\n\nReply only by calling the `{schema.__name__}` tool, exactly once."
    else:
        runnable = model.with_structured_output(schema, method="function_calling")
    error = None
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        usage = None
        try:
            out = runnable.invoke([SystemMessage(system), HumanMessage(user_prompt)])
            if mode == "auto":
                usage = getattr(out, "usage_metadata", None)
                call = next((c for c in (out.tool_calls or []) if c["name"] == schema.__name__), None)
                if call is None:
                    raise ValueError(f"model did not call {schema.__name__}")
                out = call["args"]
            parsed = out if isinstance(out, schema) else schema.model_validate(out)
            log_event("prompts", {
                "node": node, "provider": s.llm_provider, "model": s.llm_model, "attempt": attempt, "mode": mode,
                "system": system, "user": user_prompt, "output": parsed.model_dump(), "usage": usage,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
            })
            return parsed
        except Exception as e:  # noqa: BLE001 - any provider/parse error -> retry, then fall back
            error = f"{type(e).__name__}: {e}"
            if usage:       # a reply that could not be parsed was still billed: keep its token count
                log_event("prompts", {"node": node, "provider": s.llm_provider, "model": s.llm_model, "attempt": attempt,
                                      "error": error, "usage": usage})
    log_event("prompts", {"node": node, "provider": s.llm_provider, "model": s.llm_model,
                          "system": system, "user": user_prompt, "error": error})
    return None
