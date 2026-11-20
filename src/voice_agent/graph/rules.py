"""Deterministic intent extraction.

Used (a) as the Router when LLM_PROVIDER=rules or the model call fails, and
(b) as a cross-check on the LLM Router: a budget the user clearly stated is never lost.
"""
from __future__ import annotations

import re

from ..schemas import Constraints, Intent

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMWORD = "|".join(list(_TENS) + sorted(_UNITS, key=len, reverse=True) + ["hundred", "a hundred"])
_NUMSEQ = re.compile(rf"\b((?:a\s+)?(?:{_NUMWORD})(?:[\s-]+(?:{_NUMWORD}))*)\b", re.I)


def _words_to_int(phrase: str) -> int | None:
    total, current, seen = 0, 0, False
    for w in re.split(r"[\s-]+", phrase.lower()):
        if w == "a":
            continue
        if w in _UNITS:
            current += _UNITS[w]; seen = True
        elif w in _TENS:
            current += _TENS[w]; seen = True
        elif w == "hundred":
            current = (current or 1) * 100; seen = True
        else:
            return None
    return total + current if seen else None


def spoken_numbers_to_digits(text: str) -> str:
    """'under fifteen dollars' -> 'under 15 dollars' (ASR sometimes spells numbers out)."""
    def repl(m: re.Match) -> str:
        n = _words_to_int(m.group(1))
        return str(n) if n is not None else m.group(1)
    return _NUMSEQ.sub(repl, text)


_N = r"\$?\s*(\d+(?:\.\d{1,2})?)"
_CUR = r"(?:\s*(?:dollars?|bucks|usd))?"
_MAX_PATTERNS = [
    rf"\b(?:max[_ ]price|price cap|cap)\s*(?:of|to|at|is|=|:)?\s*{_N}{_CUR}",
    rf"\b(?:under|below|less than|cheaper than|at most|up to|no more than|not more than|max(?:imum)?(?: of)?|within|budget (?:of|is)(?: the| a)?)\s*{_N}{_CUR}",
    rf"{_N}{_CUR}\s*(?:or less|or under|or below|max(?:imum)?|tops|budget)\b",
]
_MIN_PATTERNS = [rf"\b(?:over|above|more than|at least|no less than|min(?:imum)?(?: of)?|starting at)\s*{_N}{_CUR}",
                 rf"{_N}{_CUR}\s*(?:or more|or above|and up)\b"]
_BETWEEN = rf"\b(?:between|from)\s*{_N}{_CUR}\s*(?:and|to|-)\s*{_N}{_CUR}"
_AROUND = rf"\b(?:around|about|roughly|approximately)\s*{_N}\s*(?:dollars?|bucks|usd)"

_LIVE = re.compile(r"\b(current(?:ly)?|latest|right now|today(?:'s)?|live|available now|in stock|still available|"
                   r"these days|at the moment|up[- ]to[- ]date)\b", re.I)
_COMPARE = re.compile(r"\b(compare|comparison|versus|vs\.?|difference between|which is better)\b", re.I)
_GREETING = re.compile(r"^\W*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening)|bye|goodbye)\b[\W\w]{0,20}$", re.I)
_OUT_OF_SCOPE = re.compile(r"\b(weather|stock market|write (me )?(a|an) (poem|essay|code)|translate|who (is|was) the president|"
                           r"medical advice|legal advice|homework|tell me a joke|place (an|my|the) order|"
                           r"order (it|this|that|the \w+ one) for me|add (it|this|that|.{0,30}) to (my )?cart|check ?out|"
                           r"coupon|promo code|reserve|refund|track my (order|package)|my order|send (me )?(a |the )?receipt)\b", re.I)
_FOLLOW_UP = re.compile(r"\b(cheaper|cheapest|pricier|more expensive|another|other (one|option)s?|instead|same|that one|"
                        r"those|the (first|second|third|last) one|highest[- ]rated|most affordable|what about)\b", re.I)
_PREFERENCES = [
    "eco-friendly", "eco friendly", "plant-based", "non-toxic", "nontoxic", "organic", "unscented", "fragrance-free",
    "biodegradable", "recycled", "bpa-free", "hypoallergenic", "educational", "durable", "lightweight", "waterproof",
    "quiet", "portable", "gift", "outdoor", "indoor", "for kids", "for toddlers", "for adults", "for babies",
]
_MATERIALS = ["stainless steel", "wooden", "wood", "plastic", "cotton", "silicone", "metal", "bamboo", "glass",
              "ceramic", "leather", "foam", "rubber", "plush", "paper"]
_AGE = re.compile(r"\b(?:for (?:an? |my )?)?(\d{1,2})[- ]?(?:year|yr)s?[- ]?olds?\b", re.I)
_BRAND = re.compile(r"\b(?:by|from|made by|brand)\s+([A-Z][\w&'\-]+(?:\s+[A-Z][\w&'\-]+)?)")
_FILLER = re.compile(
    r"\b(today's price|today's|price for|with at least|with|can you|could you|would you|please|i need|i want|i'm looking for|i am looking for|looking for|find me|find|"
    r"show me|show|recommend me|recommend|suggest|give me|get me|search for|do you have|i'd like|i would like|"
    r"help me (find|choose|pick)|what is|what's|what are|is there|are there|tell me about|the best|best|a good|good|"
    r"some|any|an|a|the|me|my|price of|current price|latest price|current|latest|right now|today|in stock|available now|"
    r"is it|still available|and|that is|that are|which is|compare|versus|vs)\b", re.I)


_RATING = re.compile(r"(?:\b(?:at least|over|above|more than|minimum(?: of)?|no less than)\s+)?"
                     r"(\d(?:\.\d)?)(\s+and a half)?[\s-]*(?:stars?|star rating)\b|\brated\s+(\d(?:\.\d)?)(\s+and a half)?", re.I)


def extract_min_rating(text: str) -> float | None:
    """'at least four and a half stars' -> 4.5"""
    m = _RATING.search(spoken_numbers_to_digits(text.lower()))
    if not m:
        return None
    value = float(m.group(1) or m.group(3)) + (0.5 if (m.group(2) or m.group(4)) else 0)
    return value if 0 < value <= 5 else None


def extract_budget(text: str) -> tuple[float | None, float | None]:
    t = _RATING.sub(" ", spoken_numbers_to_digits(text.lower()))      # "at least 4 stars" is not a price floor
    m = re.search(_BETWEEN, t)
    if m:
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        return lo, hi
    lo = hi = None
    # The LAST mention wins: people correct themselves when speaking ("fifteen, no wait, forty").
    last = max((m for p in _MAX_PATTERNS for m in re.finditer(p, t)), key=lambda m: m.start(), default=None)
    if last:
        hi = float(last.group(1))
    last = max((m for p in _MIN_PATTERNS for m in re.finditer(p, t)), key=lambda m: m.start(), default=None)
    if last:
        lo = float(last.group(1))
    if hi is None and lo is None and (m := re.search(_AROUND, t)):
        hi = round(float(m.group(1)) * 1.15, 2)
    return lo, hi


def _strip_budget(text: str) -> str:
    t = _RATING.sub(" ", spoken_numbers_to_digits(text))
    for p in [_BETWEEN, *_MAX_PATTERNS, *_MIN_PATTERNS, _AROUND]:
        t = re.sub(p, " ", t, flags=re.I)
    return t


def rule_intent(transcript: str, history: list[dict] | None = None) -> Intent:
    text = transcript.strip()
    low = text.lower()
    lo, hi = extract_budget(text)
    prefs = []
    for p in _PREFERENCES:
        if p in low and p.replace(" ", "-") not in prefs:
            prefs.append(p.replace("eco friendly", "eco-friendly").replace("nontoxic", "non-toxic"))
    if (m := _AGE.search(low)):
        prefs.append(f"for a {m.group(1)} year old")
    material = next((m for m in _MATERIALS if re.search(rf"\b{m}\b", low)), None)
    brand = (m.group(1) if (m := _BRAND.search(text)) else None)

    query = _FILLER.sub(" ", _strip_budget(text))
    query = re.sub(r"[^\w\s&'\-]", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    query = re.sub(r"^(?:(?:of|for|on|to|in|with)\s+)+", "", query, flags=re.I)      # leftover leading prepositions
    generic = {"something", "anything", "stuff", "thing", "things", "item", "items", "product", "products", "one", "ones"}
    content_tokens = [t for t in query.split() if len(t) > 2 and t.lower() not in generic]

    constraints = Constraints(product_type=query or None, max_price=hi, min_price=lo, brand=brand,
                              material=material, preferences=prefs, min_rating=extract_min_rating(text))
    wants_live = bool(_LIVE.search(low))

    if _OUT_OF_SCOPE.search(low):        # checked before follow-up handling: "order the first one" is not a search
        return Intent(task="out_of_scope")

    # follow-up: no new product named -> inherit the previous search and constraints
    prev =next((h.get("intent") for h in reversed(history or []) if h.get("intent", {}).get("search_query")), None)
    is_follow_up = prev is not None and (bool(_FOLLOW_UP.search(low)) or len(content_tokens) == 0)
    if is_follow_up:
        pc = prev.get("constraints", {})
        cheaper = re.search(r"\b(cheaper|cheapest|most affordable)\b", low)
        prev_shown = [p for h in reversed(history or []) for p in h.get("shown", []) if p.get("price")][:3]
        if hi is None and cheaper and prev_shown:
            hi = round(min(p["price"] for p in prev_shown) - 0.01, 2)
        constraints = Constraints(
            product_type=pc.get("product_type"), max_price=hi if hi is not None else pc.get("max_price"),
            min_price=lo if lo is not None else pc.get("min_price"), brand=brand or pc.get("brand"),
            material=material or pc.get("material"), category=pc.get("category"),
            preferences=list(dict.fromkeys([*pc.get("preferences", []), *prefs])),
        )
        return Intent(task="product_search", search_query=prev["search_query"], constraints=constraints,
                      wants_live_info=wants_live)

    if _GREETING.match(text):
        return Intent(task="chitchat")
    if _OUT_OF_SCOPE.search(low):
        return Intent(task="out_of_scope")
    if not content_tokens:
        return Intent(task="clarification_needed", constraints=constraints,
                      clarification_question="What kind of product are you looking for, and do you have a budget?")
    task = "product_comparison" if _COMPARE.search(low) else \
        "price_availability_check" if wants_live and re.search(r"\b(price|cost|stock|available)\b", low) else "product_search"
    return Intent(task=task, search_query=query, constraints=constraints, wants_live_info=wants_live)
