"""ProofAgent Nano runtime input guardrail: scans the user's message (typed, or the ASR transcript)
before the LangGraph Router sees it.

Contract used (official cookbook client `guards/python/guard.py`, ProofAgent-ai/agent-guardrails-cookbook,
and checked against the live API on 2026-11-22):

    POST {PROOFAGENT_NANO_URL}/v1/scan           default https://nano.proofagent.ai
    Authorization: Bearer <PROOFAGENT_NANO_KEY>
    {"text": "...", "direction": "input"}
 -> {"decision": {"action": PASS|FLAG|MASK|SANITISE|REVIEW|GUARDIAN_ALERT|BLOCK, "severity", "reasons"},
     "text": {"masked": "..."}, "request": {"id": "req_..."}, "summary": {"top_risk"}, "findings": [...],
     "report": {"summary"}, "model": "nano-1.1", ...}

The wire actions are reduced to four decisions the graph acts on, with the cookbook's mapping:
    PASS, FLAG -> pass      MASK, SANITISE -> mask      REVIEW, GUARDIAN_ALERT -> review      BLOCK -> block
An action this code does not know is treated as block (fail closed, as in the cookbook).

`source` says where a result came from, so nothing else is ever presented as a ProofAgent verdict:
    proofagent_nano  a real response from the configured Nano endpoint
    mock             a canned response injected by a test (transport=...)
    disabled         PROOFAGENT_GUARD=off: no scan was made
    error            a scan was attempted and failed (network, auth, quota, bad response); no verdict exists

The existing ShopVoice pattern guardrails (safety.check_input) are separate and still run in the Router.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

import httpx

from .config import get_settings

NANO_ACTIONS = {"PASS": "pass", "FLAG": "pass", "MASK": "mask", "SANITISE": "mask",
                "REVIEW": "review", "GUARDIAN_ALERT": "review", "BLOCK": "block"}


@dataclass
class GuardResult:
    source: str                    # proofagent_nano | mock | disabled | error
    decision: str                  # pass | mask | review | block (what the graph does)
    action: str | None = None      # the raw ProofAgent action, only when ProofAgent answered
    text: str = ""                 # text to hand to the Router (masked when decision == mask and mask mode is apply)
    masked: bool = False           # True if `text` differs from the input because of masking
    request_id: str | None = None
    severity: str | None = None
    top_risk: str | None = None
    risks: list[str] = field(default_factory=list)
    summary: str = ""
    model: str | None = None
    latency_ms: int | None = None
    http_status: int | None = None
    error: str | None = None
    note: str = ""

    @property
    def is_proofagent(self) -> bool:
        """Only a real response from the Nano API is a ProofAgent verdict."""
        return self.source == "proofagent_nano"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "is_proofagent": self.is_proofagent}


def _risks(body: dict) -> list[str]:
    out = []
    for f in body.get("findings") or []:
        if not isinstance(f, dict):
            continue
        rid = f.get("id_risk") or (f.get("family") and f.get("subfamily") and f"{f['family']}:{f['subfamily']}")
        if rid and rid not in out:
            out.append(rid)
    return out


_PLACEHOLDER = re.compile(r"\[PERSON:[A-Z_]+#\d+\]")


@lru_cache(maxsize=1)
def _catalog_brand_keys() -> tuple[str, ...]:
    """Every catalog brand as lowercase letters and digits only ("Mrs. Meyer's" -> "mrsmeyers")."""
    try:
        import pandas as pd
        brands = pd.read_parquet(get_settings().products_parquet, columns=["brand"])["brand"].dropna().unique()
    except Exception:       # no catalog built: nothing to restore against
        return ()
    return tuple(sorted({re.sub(r"[^a-z0-9]", "", str(b).lower()) for b in brands} - {""}))


def _completes_brand(before: str, span: str, after: str) -> bool:
    """True when the masked span, joined with up to two neighbouring words on each side, spells a whole catalog
    brand ("Mrs." + "Meyers" -> "mrsmeyers"). A name that merely occurs inside a brand ("John" in "Johnson") is not."""
    keys = set(_catalog_brand_keys())
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: E731
    left = [norm(t) for t in before.split()[-2:]]
    right = [norm(t) for t in after.split()[:2]]
    core = norm(span)
    if len(core) < 3:
        return False
    return any("".join(left[len(left) - i:] + [core] + right[:j]) in keys
               for i in range(len(left) + 1) for j in range(len(right) + 1))


def restore_brand_names(original: str, masked: str) -> tuple[str, list[str]]:
    """Undo PERSON masks that hide a catalog brand. Nano masks some brands as names ("Mrs. Meyers" ->
    "Mrs. [PERSON:LAST_NAME#1]"), which would remove the brand from the search. A masked word is put back only if
    it is part of a catalog brand name; real personal names, and every other kind of mask, stay masked."""
    import difflib

    a, b = original.split(), masked.split()
    out, restored = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        span_masked = " ".join(b[j1:j2])
        span_orig = " ".join(a[i1:i2])
        if op == "replace" and _PLACEHOLDER.fullmatch(span_masked.strip(".,!?;:")) \
                and _completes_brand(" ".join(a[:i1]), span_orig, " ".join(a[i2:])):
            out.append(span_orig)
            restored.append(span_orig)
        else:
            out.extend(b[j1:j2])
    return " ".join(out), restored


_ANY_PLACEHOLDER = re.compile(r"(\[[A-Z_]+:[A-Z_]+#\d+\])")
_CITATION = re.compile(r"^(https?://\S+|hh23-[0-9A-Za-z]+|amz20-[0-9a-f]+|\$\s?[\d,.]+)$")


def restore_citations(original: str, masked: str, public: tuple[str, ...] | list[str] = ()) -> tuple[str, list[str]]:
    """For the assistant's reply: undo masks over citations (product URLs, doc_ids, prices) and catalog brand names,
    keep every other mask. Layout and line breaks are preserved: the masked text is lined up with the original,
    placeholder by placeholder. If the two cannot be lined up, the masked text is returned unchanged.
    `public`: catalog text (product titles, brands) whose words are product data, e.g. a model number in a title."""
    parts = _ANY_PLACEHOLDER.split(masked)
    if len(parts) == 1:
        return masked, []
    pattern = "".join("(.+?)" if i % 2 else re.escape(p) for i, p in enumerate(parts))
    m = re.fullmatch(pattern, original, re.S)
    if not m:
        return masked, []
    out, restored, g = [], [], 1
    for i, p in enumerate(parts):
        if i % 2 == 0:
            out.append(p)
            continue
        orig = m.group(g)
        g += 1
        after = _ANY_PLACEHOLDER.sub(" ", "".join(parts[i + 1:]))
        bare = orig.strip(" []()<>.,;:")
        if _CITATION.match(bare) or (p.startswith("[PERSON:") and _completes_brand("".join(out), orig, after))                 or (len(bare) >= 3 and any(bare in t for t in public)):
            out.append(orig)
            restored.append(orig)
        else:
            out.append(p)
    return "".join(out), restored


def parse_response(body: Any, original: str, source: str, mask_mode: str, direction: str = "input") -> GuardResult:
    """Turn a /v1/scan JSON body into a GuardResult. A body without decision.action is an error, not a verdict."""
    decision = (body or {}).get("decision") if isinstance(body, dict) else None
    action = decision.get("action") if isinstance(decision, dict) else None
    if not isinstance(action, str) or not action:
        return GuardResult(source="error", decision="block", text=original, error="response has no decision.action")
    mapped = NANO_ACTIONS.get(action.upper(), "block")
    masked_text = ((body.get("text") or {}).get("masked") if isinstance(body.get("text"), dict) else None) or original
    use_masked = mapped == "mask" and mask_mode == "apply"
    note = "" if action.upper() in NANO_ACTIONS else f"unknown action {action!r}: treated as block (fail closed)"
    if mapped == "mask" and not use_masked:
        note = "MASK returned; PROOFAGENT_GUARD_MASK=observe, so the original text was passed on"
    if use_masked and masked_text != original:
        if direction == "output":
            masked_text, restored = restore_citations(original, masked_text)
            if restored:
                note = f"Citations or brand names masked by the scan were restored: {', '.join(restored)}"
        else:
            masked_text, restored = restore_brand_names(original, masked_text)
            if restored:
                note = f"Brand name(s) masked as personal data were restored: {', '.join(restored)}"
    return GuardResult(
        source=source, decision=mapped, action=action, text=masked_text if use_masked else original,
        masked=use_masked and masked_text != original,
        request_id=(body.get("request") or {}).get("id"), severity=decision.get("severity"),
        top_risk=(body.get("summary") or {}).get("top_risk"), risks=_risks(body),
        summary=(body.get("report") or {}).get("summary") or "", model=body.get("model"), note=note)


def _error(text: str, on_error: str, message: str, status: int | None = None, ms: int | None = None) -> GuardResult:
    decision = "pass" if on_error == "fallback" else "block"
    note = ("ProofAgent scan failed; continuing with ShopVoice's own pattern guardrails only (not a ProofAgent verdict)"
            if decision == "pass" else "ProofAgent scan failed; request stopped (PROOFAGENT_GUARD_ON_ERROR=block)")
    return GuardResult(source="error", decision=decision, text=text, error=message, http_status=status, latency_ms=ms, note=note)


def scan_input(text: str, *, transport: httpx.BaseTransport | None = None) -> GuardResult:
    """Scan one user message. Never raises; never returns the API key."""
    return scan(text, "input", transport=transport)


def scan(text: str, direction: str = "input", *, transport: httpx.BaseTransport | None = None) -> GuardResult:
    """Scan text with POST /v1/scan. direction "input" = the user's message, "output" = the assistant's reply."""
    s = get_settings()
    checkpoint = "input" if direction == "input" else "output"
    if s.guard_mode != "on" or checkpoint not in {c.strip() for c in s.guard_checkpoints.split(",")}:
        return GuardResult(source="disabled", decision="pass", text=text,
                           note=f"ProofAgent {checkpoint} scan off (PROOFAGENT_GUARD / PROOFAGENT_GUARD_CHECKPOINTS)")
    if not text.strip():
        return GuardResult(source="disabled", decision="pass", text=text, note="empty input: nothing to scan")
    if not s.nano_key and transport is None:
        return _error(text, s.guard_on_error, "PROOFAGENT_NANO_KEY is not set")
    source = "mock" if transport is not None else "proofagent_nano"
    t0 = time.perf_counter()
    try:
        with httpx.Client(transport=transport, timeout=s.guard_timeout) as client:
            r = client.post(f"{s.nano_url}/v1/scan", json={"text": text, "direction": direction},
                            headers={"Authorization": f"Bearer {s.nano_key}"})
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code >= 300:
            try:
                err = (r.json().get("error") or {})
                message = f"HTTP {r.status_code} {err.get('code', '')}".strip()
            except ValueError:
                message = f"HTTP {r.status_code}"
            return _error(text, s.guard_on_error, message, r.status_code, ms)
        result = parse_response(r.json(), text, source, s.guard_mask_mode, direction)
    except httpx.TimeoutException:
        return _error(text, s.guard_on_error, f"timeout after {s.guard_timeout:g} s", ms=round((time.perf_counter() - t0) * 1000))
    except (httpx.HTTPError, ValueError) as e:
        return _error(text, s.guard_on_error, f"{type(e).__name__}", ms=round((time.perf_counter() - t0) * 1000))
    if result.source == "error":
        return _error(text, s.guard_on_error, result.error or "invalid response", r.status_code, ms)
    result.http_status, result.latency_ms = r.status_code, ms
    return result


def log_record(result: GuardResult, original: str) -> dict[str, Any]:
    """What goes to logs/input_guard.jsonl: the verdict and a hash of the input, never the key or the raw text."""
    d = result.to_dict()
    d.pop("text", None)
    d["input_sha256"] = hashlib.sha256(original.encode()).hexdigest()[:16]
    d["input_chars"] = len(original)
    return d
