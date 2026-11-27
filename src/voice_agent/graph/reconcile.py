"""Match live web results to catalog products and surface conflicts.

Matching: SKU/ASIN in the URL or text wins outright. Otherwise the live title must be (almost) fully
contained in the catalog title: shared brand and category words ("Clorox ... Disinfecting ... Wipes")
are not enough, because sibling products share them. Each live result goes to the product it fits best.

A product usually has several live offers (different shops, different prices). All matching offers are
kept; the one reported as "the live price" is the lowest plausible price, preferring offers that say the
item is in stock. A price that is wildly different from the catalog price is treated as a parsing error or
a different listing (a multipack, a bundle) and is not used.

A price difference above CONFLICT_THRESHOLD is reported, never silently resolved: the catalog price is
a 2023 snapshot, the live price is newer, and both are shown.
"""
from __future__ import annotations

import re
from typing import Any

MATCH_THRESHOLD = 0.75      # share of the live title's content words that must occur in the catalog title
CONFLICT_THRESHOLD = 0.10   # relative price difference that counts as a conflict
PLAUSIBLE_RATIO = 4.0       # a live price more than 4x or less than 1/4 of the catalog price is not trusted

_TOKEN = re.compile(r"[a-z0-9]+")
_ASIN = re.compile(r"\bb0[a-z0-9]{8}\b", re.I)      # Amazon product id as it appears in URLs and listings
_NOISE = set("the a an and of for with in on to by amazon com price buy new pack set best deals shop online "
             "toys games walmart target ebay sale free shipping".split())


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _NOISE and len(t) > 1}


def title_similarity(catalog_title: str, live_title: str) -> float:
    """Share of the live title's content words found in the catalog title (0..1)."""
    cat, live = _tokens(catalog_title), _tokens(live_title)
    if not cat or len(live) < 2:
        return 0.0
    return len(cat & live) / len(live)


def match_score(product: dict[str, Any], web: dict[str, Any]) -> tuple[float, str]:
    haystack = f"{web.get('url', '')} {web.get('title', '')} {web.get('snippet', '')}".lower()
    sku = product.get("sku")
    if sku and sku.lower() in haystack:
        return 1.0, "sku"
    other_ids = {a.upper() for a in _ASIN.findall(haystack)}
    if sku and other_ids:
        return 0.0, "different sku"      # the live page names another product id: similar title, different item (e.g. a 4-pack)
    return title_similarity(product.get("title", ""), web.get("title", "")), "title"


def _plausible(live_price: float | None, catalog_price: float | None) -> bool:
    if live_price is None:
        return False
    if not catalog_price:
        return True
    return catalog_price / PLAUSIBLE_RATIO <= live_price <= catalog_price * PLAUSIBLE_RATIO


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def reconcile(rag_results: list[dict], web_results: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """-> (evidence, conflicts, unmatched_live_results)"""
    evidence = [dict(p) for p in rag_results]
    conflicts: list[dict] = []

    # each live result is assigned to the single product it matches best
    assigned: dict[int, list[tuple[float, str, dict]]] = {}
    used: set[int] = set()
    for wi, web in enumerate(web_results):
        if web.get("suspicious"):
            continue
        best, best_pi, best_how = 0.0, None, ""
        for pi, product in enumerate(evidence):
            score, how = match_score(product, web)
            if score >= MATCH_THRESHOLD and score > best:
                best, best_pi, best_how = score, pi, how
        if best_pi is not None:
            assigned.setdefault(best_pi, []).append((best, best_how, web))
            used.add(wi)

    for pi, matches in assigned.items():
        product = evidence[pi]
        offers = []
        for score, how, web in matches:
            price = web.get("price") if _plausible(web.get("price"), product.get("price")) else None
            offers.append({"price": price, "url": web.get("url"), "domain": _domain(web.get("url", "")),
                           "availability": web.get("availability"), "title": web.get("title"),
                           "matched_by": how, "match_score": round(score, 2),
                           "price_rejected": web.get("price") if price is None and web.get("price") is not None else None})
        priced = [o for o in offers if o["price"] is not None]
        # the offer reported as "the live price": in stock first, then the lowest price, then the best match
        primary = min(priced, key=lambda o: (o["availability"] != "in_stock", o["price"], -o["match_score"])) if priced \
            else max(offers, key=lambda o: o["match_score"])
        prices = sorted({o["price"] for o in priced})
        product["live"] = {
            "title": primary["title"], "url": primary["url"], "price": primary["price"],
            "availability": primary["availability"], "matched_by": primary["matched_by"], "match_score": primary["match_score"],
            "offers": [{k: o[k] for k in ("price", "domain", "url", "availability")} for o in offers],
            "price_range": [prices[0], prices[-1]] if len(prices) > 1 else None,
        }
        cat_price, live_price = product.get("price"), primary["price"]
        if cat_price and live_price and abs(live_price - cat_price) / cat_price > CONFLICT_THRESHOLD:
            conflicts.append({
                "doc_id": product["doc_id"], "title": product["title"], "field": "price",
                "catalog_value": cat_price, "live_value": live_price, "live_url": primary["url"],
                "live_range": product["live"]["price_range"], "live_sources": len(priced),
                "resolution": "Catalog price is a 2023 snapshot; the live price is newer. Both are shown and "
                              "budget fit is judged on the live price when one exists.",
            })
        if primary["availability"] == "out_of_stock" or (not priced and any(o["availability"] == "out_of_stock" for o in offers)):
            conflicts.append({
                "doc_id": product["doc_id"], "title": product["title"], "field": "availability",
                "catalog_value": "listed", "live_value": "out_of_stock", "live_url": primary["url"],
                "resolution": "Listed in the catalog but reported unavailable by the live source.",
            })

    unmatched = [w for i, w in enumerate(web_results) if i not in used]
    return evidence, conflicts, unmatched
