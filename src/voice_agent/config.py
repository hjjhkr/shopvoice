"""Central configuration. Everything that varies between deployments is an
environment variable (see .env.example), so the LLM, embeddings, ASR, TTS and
web-search provider can be swapped without code changes."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _path(name: str, default: str) -> Path:
    p = Path(_env(name, default))
    return p if p.is_absolute() else ROOT / p


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip().lower() for x in _env(name).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # --- LLM (model-agnostic: any LangChain chat-model provider) ---
    llm_provider: str   # openai | anthropic | ollama | google_genai | bedrock | rules
    llm_model: str
    llm_temperature: float | None   # None = provider default (nothing is sent)
    llm_base_url: str
    # --- Embeddings / index ---
    embedding_provider: str  # local (ONNX MiniLM) | ollama | openai
    embedding_model: str
    raw_csv: Path
    products_parquet: Path
    chroma_dir: Path
    collection: str
    category_regex: str
    # --- web.search ---
    web_provider: str   # brave | serper | tavily | fixture | none
    web_cache_ttl: int
    web_rate_per_min: int
    web_allowed_domains: tuple[str, ...]
    web_blocked_domains: tuple[str, ...]
    web_fixture_path: Path
    # --- Speech ---
    asr_provider: str   # faster_whisper | openai
    asr_model: str
    tts_provider: str   # pyttsx3 | openai
    tts_model: str
    tts_voice: str
    # --- MCP ---
    mcp_transport: str  # stdio | http
    mcp_url: str
    # --- Logs / outputs ---
    log_dir: Path
    audio_dir: Path
    # --- ProofAgent Nano runtime input guardrail (see docs/INPUT_GUARD.md) ---
    guard_mode: str = "off"          # on | off
    nano_url: str = "https://nano.proofagent.ai"
    nano_key: str = ""
    guard_timeout: float = 10.0
    guard_on_error: str = "block"    # block (fail closed) | fallback (continue with ShopVoice's own guardrails only)
    guard_mask_mode: str = "apply"   # apply (Router sees masked text) | observe (verdict logged, original text passed on)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        llm_provider=_env("LLM_PROVIDER", "rules").lower(),
        llm_model=_env("LLM_MODEL", ""),
        llm_temperature=float(_env("LLM_TEMPERATURE")) if _env("LLM_TEMPERATURE") else None,
        llm_base_url=_env("LLM_BASE_URL"),
        embedding_provider=_env("EMBEDDING_PROVIDER", "local").lower(),
        embedding_model=_env("EMBEDDING_MODEL", ""),
        raw_csv=_path("RAW_CSV", "data/raw/amazon_products_2020.csv"),
        products_parquet=_path("PRODUCTS_PARQUET", "data/processed/products.parquet"),
        chroma_dir=_path("CHROMA_DIR", "data/chroma"),
        collection=_env("CHROMA_COLLECTION", "products"),
        category_regex=_env("CATEGORY_REGEX", ""),
        web_provider=_env("WEB_SEARCH_PROVIDER", "none").lower(),
        web_cache_ttl=int(_env("WEB_CACHE_TTL_SECONDS", "180")),
        web_rate_per_min=int(_env("WEB_RATE_LIMIT_PER_MIN", "20")),
        web_allowed_domains=_csv("WEB_ALLOWED_DOMAINS"),
        web_blocked_domains=_csv("WEB_BLOCKED_DOMAINS"),
        web_fixture_path=_path("WEB_FIXTURE_PATH", "eval/fixtures/web_fixture.json"),
        asr_provider=_env("ASR_PROVIDER", "faster_whisper").lower(),
        asr_model=_env("ASR_MODEL", "base.en"),
        tts_provider=_env("TTS_PROVIDER", "pyttsx3").lower(),
        tts_model=_env("TTS_MODEL", "gpt-4o-mini-tts"),
        tts_voice=_env("TTS_VOICE", ""),
        mcp_transport=_env("MCP_TRANSPORT", "stdio").lower(),
        mcp_url=_env("MCP_URL", "http://127.0.0.1:8765/mcp"),
        log_dir=_path("LOG_DIR", "logs"),
        audio_dir=_path("AUDIO_DIR", "logs/audio"),
        guard_mode=_env("PROOFAGENT_GUARD", "off").lower(),
        nano_url=_env("PROOFAGENT_NANO_URL", "https://nano.proofagent.ai").rstrip("/"),
        nano_key=_env("PROOFAGENT_NANO_KEY") or _env("PROOFAGENT_NANO_API_KEY"),
        guard_timeout=float(_env("PROOFAGENT_GUARD_TIMEOUT", "10")),
        guard_on_error=_env("PROOFAGENT_GUARD_ON_ERROR", "block").lower(),
        guard_mask_mode=_env("PROOFAGENT_GUARD_MASK", "apply").lower(),
    )


def reload_settings() -> Settings:
    """Re-read the environment (used by tests and the eval runner)."""
    get_settings.cache_clear()
    return get_settings()
