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
import time
from dataclasses import asdict, dataclass, field
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


def parse_response(body: Any, original: str, source: str, mask_mode: str) -> GuardResult:
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
    s = get_settings()
    if s.guard_mode != "on":
        return GuardResult(source="disabled", decision="pass", text=text, note="PROOFAGENT_GUARD is off: no ProofAgent scan")
    if not text.strip():
        return GuardResult(source="disabled", decision="pass", text=text, note="empty input: nothing to scan")
    if not s.nano_key and transport is None:
        return _error(text, s.guard_on_error, "PROOFAGENT_NANO_KEY is not set")
    source = "mock" if transport is not None else "proofagent_nano"
    t0 = time.perf_counter()
    try:
        with httpx.Client(transport=transport, timeout=s.guard_timeout) as client:
            r = client.post(f"{s.nano_url}/v1/scan", json={"text": text, "direction": "input"},
                            headers={"Authorization": f"Bearer {s.nano_key}"})
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code >= 300:
            try:
                err = (r.json().get("error") or {})
                message = f"HTTP {r.status_code} {err.get('code', '')}".strip()
            except ValueError:
                message = f"HTTP {r.status_code}"
            return _error(text, s.guard_on_error, message, r.status_code, ms)
        result = parse_response(r.json(), text, source, s.guard_mask_mode)
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
