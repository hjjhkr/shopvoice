"""Deterministic guardrails. They run in addition to the LLM's own judgement
(Router safety flag, Critic), so a safety decision never depends on a model alone.

  check_input()        before the Router LLM   - hard blocks + flags
  sanitize_tool_text() on every tool result    - tool output is data, not instructions
  check_output()       before speaking         - redaction, unsupported-claim patterns
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logging_utils import redact

ALLOWED_TOOLS = ("rag.search", "web.search")

# --- input -------------------------------------------------------------------
_HARM = [
    (r"\b(make|making|create|produce|synthesi[sz]e|build|generate)\b.{0,50}\b(poison|toxic gas|chlorine gas|mustard gas|"
     r"chloramine|explosive|bomb|napalm|thermite|nerve agent|weapon)\b", "harmful_intent"),
    (r"\b(poison|hurt|harm|kill|injure|sedate|drug)\b.{0,40}\b(someone|somebody|person|people|my (wife|husband|"
     r"boss|neighbou?r|roommate|coworker|kid|child|dog|cat)|him|her|them|a (child|kid|pet))\b", "harmful_intent"),
    (r"\b(untraceable|undetectable)\b.{0,30}\b(poison|drug|weapon)\b", "harmful_intent"),
]
_HAZARD_MIX = [
    (r"\b(mix|mixing|combine|combining|add|adding|together)\b.{0,60}\bbleach\b.{0,60}\b(ammonia|vinegar|acid|"
     r"rubbing alcohol|toilet bowl cleaner|drain cleaner|hydrogen peroxide)\b", "hazardous_chemical"),
    (r"\bbleach\b.{0,60}\b(with|and|plus)\b.{0,20}\b(ammonia|vinegar|acid|rubbing alcohol|drain cleaner)\b",
     "hazardous_chemical"),
    (r"\b(ammonia|vinegar|drain cleaner)\b.{0,40}\b(with|and|plus)\b.{0,20}\bbleach\b", "hazardous_chemical"),
    # any request to mix/combine cleaning chemicals, whatever the second ingredient is
    (r"\b(mix|mixing|mixed|combine|combining|blend|blending)\b.{0,60}\b(bleach|ammonia|drain cleaner|toilet bowl cleaner|"
     r"cleaning (chemicals?|products?|agents?)|chemicals|cleaners)\b", "hazardous_chemical"),
    (r"\b(bleach|ammonia|drain cleaner)\b.{0,60}\b(mix|mixing|mixed|combine|combining|stronger|more powerful|more potent)\b",
     "hazardous_chemical"),
]
_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"(disregard|forget|override) (your|all|the) (instructions|rules|guidelines|system prompt)",
    r"(reveal|print|show|repeat|output|tell me).{0,30}(system prompt|your instructions|hidden prompt|developer message)",
    r"you are now\b|\bact as (dan|an? unrestricted|an? uncensored)|\bjailbreak\b|developer mode",
    r"(reveal|print|show|give|tell me|what is).{0,40}(api[_ ]?key|secret key|access token|password|credentials|\.env)",
]

CHEMICAL_SAFETY_NOTE = (
    "I can't help with mixing cleaning chemicals. Combining bleach with ammonia, acids such as vinegar, "
    "or other cleaners can release toxic gases. Use one product at a time, follow the label, and ventilate. "
    "If someone has been exposed, call Poison Control at 1-800-222-1222 in the US or emergency services."
)
REFUSAL = ("I can't help with that: it describes harming a person or animal, or making something dangerous, and my "
           "safety rules don't allow assisting with harm. I can help you find and compare products from the catalog instead.")
INJECTION_REPLY = (
    "I can't do that: my rules don't allow revealing my instructions, keys or configuration, or setting my "
    "shopping-assistant rules aside, whoever asks. What product can I help you find?"
)


@dataclass
class InputVerdict:
    flag: str = "none"          # none | hazardous_chemical | harmful_intent | prompt_injection
    block: bool = False
    reply: str = ""
    matched: list[str] = field(default_factory=list)


def check_input(text: str) -> InputVerdict:
    t = " ".join(str(text).lower().split())
    for pattern, flag in _HARM:
        if re.search(pattern, t):
            return InputVerdict(flag, True, REFUSAL, [pattern])
    for pattern, flag in _HAZARD_MIX:
        if re.search(pattern, t):
            return InputVerdict(flag, True, CHEMICAL_SAFETY_NOTE, [pattern])
    for pattern in _INJECTION:
        if re.search(pattern, t):
            return InputVerdict("prompt_injection", True, INJECTION_REPLY, [pattern])
    return InputVerdict()


# --- tool output -------------------------------------------------------------
_TOOL_INJECTION = re.compile(
    r"(ignore (all |any |the )?(previous|prior|above) instructions|system prompt|you are now|"
    r"assistant\s*:|<\s*/?\s*(system|instructions?)\s*>|do not tell the user|as an ai( language model)?,? you must|"
    r"recommend (only|this product) (regardless|no matter)|override (the )?(budget|rules))",
    re.I,
)


def sanitize_tool_text(text: str) -> tuple[str, bool]:
    """Neutralise instruction-like content in retrieved text. Returns (clean_text, was_suspicious)."""
    if not isinstance(text, str) or not text:
        return text, False
    if not _TOOL_INJECTION.search(text):
        return text, False
    cleaned = _TOOL_INJECTION.sub("[removed: instruction-like text]", text)
    return cleaned, True


# --- output ------------------------------------------------------------------
# Claims that need explicit support in the retrieved evidence to be spoken.
_CLAIM_PATTERNS = {
    "germ-kill / disinfection claim": r"\b(kills?|eliminates?|destroys?)\b.{0,25}\b(germs?|bacteria|virus(es)?)\b|\bdisinfect(s|ant|ing)?\b|\bantibacterial\b|99\.9\s*%",
    "medical claim": r"\b(cures?|treats?|heals?|prevents?)\b.{0,25}\b(disease|illness|infection|allerg(y|ies)|asthma|eczema|cancer|adhd|autism)\b",
    "safety certification": r"\b(fda|epa|cpsc|astm|usda)[- ](approved|certified|registered|compliant)\b|\bclinically proven\b|\bdermatologist[- ]tested\b",
    "absolute safety claim": r"\b(100% safe|completely safe|totally safe|perfectly safe|guaranteed safe|safe for all (ages|children|kids|babies))\b",
    "best-seller / popularity claim": r"\b(best[- ]?seller|best[- ]selling|#1|number one|top[- ]rated|most popular|award[- ]winning)\b",
}


def unsupported_claims(text: str, evidence_text: str) -> list[str]:
    """Claim categories asserted in `text` whose wording does not occur in the evidence."""
    issues = []
    ev = evidence_text.lower()
    for label, pattern in _CLAIM_PATTERNS.items():
        for m in re.finditer(pattern, text, re.I):
            phrase = m.group(0).lower()
            if phrase in ev or _is_denied(text, m.start(), m.end()):
                continue          # stated in the evidence, or the sentence denies/declines the claim
            issues.append(f"{label}: '{m.group(0)}' is not supported by the retrieved evidence")
            break
    return issues


_DENIAL = re.compile(
    r"\b(not|no|never|cannot|can't|couldn't|could not|unable|isn't|aren't|wasn't|doesn't|don't|didn't|won't|without|"
    r"unverified|unconfirmed|neither|nor|none|nothing|lacks?|missing|absent)\b", re.I)


def _is_denied(text: str, start: int, end: int) -> bool:
    """True when the claim phrase sits in a sentence that negates it, e.g. 'I did not say it is ASTM
    certified' or 'best-seller status could not be verified'. Correcting a false premise must name it."""
    left = max(text.rfind(".", 0, start), text.rfind("!", 0, start), text.rfind("?", 0, start), text.rfind("\n", 0, start)) + 1
    stops = [i for i in (text.find(".", end), text.find("!", end), text.find("?", end), text.find("\n", end)) if i != -1]
    sentence = text[left:(min(stops) if stops else len(text))]
    return bool(_DENIAL.search(sentence))


def check_output(text: str) -> str:
    """Last line of defence before text is shown or spoken."""
    return redact(text)
