"""web.search backend: provider adapters + TTL cache + rate limiter + normalisation.

Providers (WEB_SEARCH_PROVIDER):
  brave    https://api.search.brave.com   (BRAVE_API_KEY)
  serper   https://google.serper.dev      (SERPER_API_KEY)
  tavily   https://api.tavily.com         (TAVILY_API_KEY)
  fixture  canned results from a JSON file - for tests and evaluation only
  none     tool reports that live search is not configured
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import Settings
from ..safety import sanitize_tool_text

_PRICE = re.compile(r"(?:US\s*)?\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)")
_IN_STOCK = re.compile(r"\b(in stock|available now|ships today|add to cart)\b", re.I)
_OUT_STOCK = re.compile(r"\b(out of stock|currently unavailable|sold out|discontinued|no longer available)\b", re.I)


class TTLCache:
    def __init__(self, ttl_seconds: int, max_items: int = 512):
        self.ttl, self.max_items = ttl_seconds, max_items
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str):
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        self._data.pop(key, None)
        return None

    def set(self, key: str, value: Any) -> None:
        if len(self._data) >= self.max_items:
            self._data.pop(min(self._data, key=lambda k: self._data[k][0]))
        self._data[key] = (time.monotonic(), value)


class RateLimiter:
    """Sliding window: at most `per_minute` upstream calls in any 60 s."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._calls: deque[float] = deque()

    def try_acquire(self) -> float:
        """Return 0 if a call may proceed now, else seconds until a slot frees up."""
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()
        if len(self._calls) < self.per_minute:
            self._calls.append(now)
            return 0.0
        return 60 - (now - self._calls[0])


_THRESHOLD_BEFORE = re.compile(r"(free shipping|ships free|shipping|orders?|over|spend|save|coupon|gift card|minimum|credit)[^.$]{0,30}$", re.I)
_THRESHOLD_AFTER = re.compile(r"\s*(off\b|or more|\+|and up|minimum|shipping|in savings)", re.I)
_PRICE_CUE = re.compile(r"(price|now|sale|only|msrp)[^$]{0,15}$", re.I)


def extract_price(text: str) -> float | None:
    """The selling price mentioned in a title/snippet, or None.

    Real snippets mix several dollar amounts ("Free Shipping When You Order $75. Regular price $19.99"),
    so an amount is skipped when its context marks it as a shipping threshold, a discount or a coupon, and
    an amount introduced by "price", "now", "sale" or "only" is preferred over a bare one."""
    text = text or ""
    first = None
    for m in _PRICE.finditer(text):
        before, after = text[max(0, m.start() - 45):m.start()], text[m.end():m.end() + 25]
        if _THRESHOLD_AFTER.match(after) or (_THRESHOLD_BEFORE.search(before) and not _PRICE_CUE.search(before)):
            continue
        value = float(m.group(1).replace(",", ""))
        if _PRICE_CUE.search(before):
            return value
        if first is None:
            first = value
    return first


def extract_availability(text: str) -> str | None:
    if _OUT_STOCK.search(text or ""):
        return "out_of_stock"
    if _IN_STOCK.search(text or ""):
        return "in_stock"
    return None


_NOT_A_SOURCE = {"tavily.com"}      # the search provider's own entity cards are not retailer pages


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _domain_ok(url: str, settings: Settings) -> bool:
    d = _domain(url)
    if not d or urlparse(url).scheme not in ("http", "https"):
        return False
    if d in _NOT_A_SOURCE:
        return False
    if any(d == b or d.endswith("." + b) for b in settings.web_blocked_domains):
        return False
    if settings.web_allowed_domains:
        return any(d == a or d.endswith("." + a) for a in settings.web_allowed_domains)
    return True


def normalize(items: list[dict[str, Any]], settings: Settings, max_results: int) -> list[dict[str, Any]]:
    """-> [{title, url, snippet, price?, availability?, source_domain, suspicious}]"""
    out = []
    for it in items:
        url = it.get("url") or ""
        if not _domain_ok(url, settings):
            continue
        title, s1 = sanitize_tool_text(str(it.get("title") or "")[:200])
        snippet, s2 = sanitize_tool_text(str(it.get("snippet") or "")[:400])
        price = it.get("price")
        if isinstance(price, str):
            price = extract_price(price if "$" in price else f"${price}")
        if price is None:
            price = extract_price(f"{title} {snippet}")
        out.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "price": price,
            "availability": it.get("availability") or extract_availability(f"{title} {snippet}"),
            "source_domain": _domain(url),
            "suspicious": s1 or s2,
        })
        if len(out) >= max_results:
            break
    return out


async def _brave(query: str, count: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count, "country": "us"},
            headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
        )
        r.raise_for_status()
        results = r.json().get("web", {}).get("results", [])
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description")} for x in results]


async def _serper(query: str, count: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": count, "gl": "us"},
            headers={"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"},
        )
        r.raise_for_status()
        results = r.json().get("organic", [])
    return [
        {"title": x.get("title"), "url": x.get("link"), "snippet": x.get("snippet"),
         "price": (str(x["price"]) if x.get("price") is not None else None)}
        for x in results
    ]


def parse_tavily(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Tavily /search response -> the common {title, url, snippet} shape (`content` is the snippet)."""
    return [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content")} for x in data.get("results") or []]


async def _tavily(query: str, count: int, settings: Settings) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"query": query, "max_results": count, "search_depth": "basic", "topic": "general",
                            "country": "united states"}
    if settings.web_allowed_domains:        # let the provider filter upstream too; normalize() re-checks locally
        body["include_domains"] = list(settings.web_allowed_domains)
    if settings.web_blocked_domains:
        body["exclude_domains"] = list(settings.web_blocked_domains)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post("https://api.tavily.com/search", json=body,
                              headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}",
                                       "Content-Type": "application/json"})
        r.raise_for_status()
    return parse_tavily(r.json())


PROVIDER_KEYS = {"brave": "BRAVE_API_KEY", "serper": "SERPER_API_KEY", "tavily": "TAVILY_API_KEY"}
_HTTP_HINTS = {401: "the API key is missing or invalid", 403: "the API key is not allowed to do this",
               429: "the provider's rate limit was hit", 432: "the plan's usage limit is exhausted",
               433: "the pay-as-you-go limit is exhausted"}


def provider_ready(settings: Settings) -> bool:
    """True when live search can run: a real provider with its key, or the test fixture."""
    p = settings.web_provider
    return p == "fixture" or (p in PROVIDER_KEYS and bool(os.environ.get(PROVIDER_KEYS[p], "").strip()))


def _fixture(query: str, settings: Settings) -> list[dict[str, Any]]:
    """Canned results: every entry whose `match` keywords all occur in the query."""
    if not settings.web_fixture_path.exists():
        return []
    entries = json.loads(settings.web_fixture_path.read_text(encoding="utf-8"))
    q = query.lower()
    out: list[dict[str, Any]] = []
    for entry in entries:
        if all(k.lower() in q for k in entry.get("match", [])):
            out.extend(entry.get("results", []))
    return out


class WebSearch:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache = TTLCache(settings.web_cache_ttl)
        self.limiter = RateLimiter(settings.web_rate_per_min)

    def configured(self) -> bool:
        return provider_ready(self.settings)

    async def search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        provider = self.settings.web_provider
        max_results = max(1, min(int(max_results), 10))
        meta: dict[str, Any] = {"provider": provider, "cache_hit": False, "query": query}
        if not self.configured():
            return {"results": [], "meta": meta, "error":
                    "web.search is not configured (set WEB_SEARCH_PROVIDER and its API key)"}

        key = f"{provider}|{query.strip().lower()}|{max_results}"
        cached = self.cache.get(key)
        if cached is not None:
            return {"results": cached, "meta": {**meta, "cache_hit": True}}

        wait = self.limiter.try_acquire()
        if wait > 0:
            if wait > 5:
                return {"results": [], "meta": meta,
                        "error": f"rate limit reached ({self.settings.web_rate_per_min}/min); retry in {wait:.0f}s"}
            await asyncio.sleep(wait)
            self.limiter.try_acquire()

        try:
            if provider == "brave":
                raw = await _brave(query, max_results)
            elif provider == "serper":
                raw = await _serper(query, max_results)
            elif provider == "tavily":
                raw = await _tavily(query, max_results, self.settings)
            else:
                raw = _fixture(query, self.settings)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            hint = f" ({_HTTP_HINTS[code]})" if code in _HTTP_HINTS else ""
            return {"results": [], "meta": meta, "error": f"{provider} returned HTTP {code}{hint}"}
        except httpx.HTTPError as e:
            return {"results": [], "meta": meta, "error": f"{provider} request failed: {type(e).__name__}"}

        results = normalize(raw, self.settings, max_results)
        self.cache.set(key, results)
        return {"results": results, "meta": meta}
