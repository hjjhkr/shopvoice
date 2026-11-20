"""Retrieval benchmark for rag.search: how good is the private-catalog retrieval, and does each part of the
hybrid design earn its place?

    python eval/run_retrieval_eval.py                  # all modes, writes eval/results/retrieval_eval.{json,md}
    python eval/run_retrieval_eval.py --modes hybrid   # one mode
    python eval/run_retrieval_eval.py --show wooden_puzzle_toddler   # print the top 10 for one query

Calls ProductStore.search directly (no LLM, no MCP transport), so it is fast and deterministic.
Queries and relevance criteria: eval/retrieval/queries.yaml (see the header there for how relevance is defined).

Ranking quality, per mode (dense | bm25 | hybrid_rrf | hybrid):
    known_item : Hit@1, Hit@5, MRR@10 for the product, reported separately for queries that name it and queries
                 that only describe it (`descriptive: true`)
    attribute  : P@5 (share of the top 5 that is the right kind of product; top min(5, pool) when the catalog
                 has fewer than 5 relevant products) and nDCG@10 (graded, ideal = best
                 possible ordering of every relevant product in the catalog that passes the filters)
Relevance labels (production mode only; the agent keeps only `strong` hits as evidence):
    strong precision / recall over the top 5 of attribute queries
    missed match   : a relevant product is in the top 5 but none of the relevant ones is labelled strong,
                     so the agent would say "not in the catalog" although it is
    false match    : a no-match query gets a `strong` hit in the top 5
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TOP_K = 10
MODES = ("dense", "bm25", "hybrid_rrf", "hybrid")
FILTER_KEYS = ("max_price", "min_price", "brand", "category", "min_rating")


# ------------------------------------------------------------------ relevance (independent of every retriever)
class Judge:
    """Grades catalog products against the criteria in queries.yaml: 0 = not relevant, 1 = right kind of product,
    2 = also meets the `ideal` preferences (or no preferences were given)."""

    def __init__(self, store):
        df = store.df
        self.store = store
        self.fields = {
            "title": df["title"].fillna("").str.lower(),
            "text": (df["title"].fillna("") + " " + df["features"].fillna("") + " "
                     + df["description"].fillna("")).str.lower(),
            "category": df["category"].fillna("").str.lower(),
        }
        self.age = df["age_min_years"]

    def _match(self, spec: dict) -> pd.Series:
        mask = pd.Series(True, index=self.age.index)
        for key, pats in (spec or {}).items():
            if key == "age_max":
                mask &= self.age.fillna(99) <= float(pats)
            elif key == "any":                                   # OR of sub-specs, e.g. title OR category
                alt = pd.Series(False, index=self.age.index)
                for sub in pats:
                    alt |= self._match(sub)
                mask &= alt
            else:
                rx = "|".join(f"(?:{p})" for p in pats)
                mask &= self.fields[key].str.contains(rx, case=False, regex=True)
        return mask

    def grades(self, q: dict) -> pd.Series:
        filters = {k: q.get("filters", {}).get(k) for k in FILTER_KEYS}
        passes = self.store._mask(**filters)                   # the hard constraints the agent applies
        rel = self._match(q["relevant"]) & passes
        if q.get("exclude_title"):
            rel &= ~self.fields["title"].str.contains("|".join(f"(?:{p})" for p in q["exclude_title"]), regex=True)
        if q.get("exclude_category"):
            rel &= ~self.fields["category"].str.contains(
                "|".join(f"(?:{p})" for p in q["exclude_category"]), case=False, regex=True)
        ideal = self._match(q["ideal"]) if q.get("ideal") else pd.Series(True, index=rel.index)
        return (rel.astype(int) + (rel & ideal).astype(int)).rename("grade")


# ------------------------------------------------------------------ metrics
def dcg(gains: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def score_query(q: dict, results: list[dict], grades: pd.Series | None) -> dict:
    ids = [r["doc_id"] for r in results]
    if q["type"] == "known_item":
        targets = set(q["targets"])
        rank = next((i + 1 for i, d in enumerate(ids) if d in targets), None)
        return {"hit1": float(rank == 1), "hit5": float(bool(rank) and rank <= 5), "mrr": 1.0 / rank if rank else 0.0,
                "rank": rank}
    if q["type"] == "attribute":
        gains = [int(grades.get(d, 0)) for d in ids]
        ideal = sorted(grades[grades > 0].tolist(), reverse=True)[:TOP_K]
        pool = int((grades > 0).sum())
        k = min(5, pool) or 5           # a query with 3 relevant products in the catalog can at best reach 3/5
        return {"p5": sum(g > 0 for g in gains[:k]) / k, "ndcg10": dcg(gains) / dcg(ideal) if ideal else 0.0,
                "pool": pool}
    return {}


def label_stats(q: dict, results: list[dict], grades: pd.Series | None) -> dict:
    top5 = results[:5]
    strong = [r for r in top5 if r.get("relevance") == "strong"]
    if q["type"] == "no_match":
        return {"false_match": bool(strong), "strong_titles": [r["title"][:60] for r in strong]}
    if q["type"] == "known_item":
        hit = next((r for r in top5 if r["doc_id"] in set(q["targets"])), None)
        return {"target_in_top5": bool(hit), "target_strong": bool(hit and hit.get("relevance") == "strong")}
    rel = [r for r in top5 if grades.get(r["doc_id"], 0) > 0]
    return {"strong_rel": sum(1 for r in strong if grades.get(r["doc_id"], 0) > 0),
            "strong_irrel": sum(1 for r in strong if grades.get(r["doc_id"], 0) == 0),
            "weak_rel": sum(1 for r in rel if r.get("relevance") != "strong"),
            "missed_match": bool(rel) and not any(r.get("relevance") == "strong" for r in rel)}


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


# ------------------------------------------------------------------ run
def run(modes, query_file: Path) -> dict:
    from voice_agent.rag.store import ProductStore

    queries = yaml.safe_load(query_file.read_text(encoding="utf-8"))["queries"]
    t0 = time.perf_counter()
    store = ProductStore()
    load_s = time.perf_counter() - t0
    judge = Judge(store)
    grades = {q["id"]: judge.grades(q) for q in queries if q["type"] == "attribute"}

    per_query: dict[str, dict] = {q["id"]: {"type": q["type"], "query": q["query"],
                                            "descriptive": bool(q.get("descriptive"))} for q in queries}
    summary: dict[str, dict] = {}
    for mode in modes:
        lat = []
        for q in queries:
            filters = {k: v for k, v in (q.get("filters") or {}).items() if k in FILTER_KEYS}
            t = time.perf_counter()
            res = store.search(q["query"], top_k=TOP_K, mode=mode, **filters)
            lat.append((time.perf_counter() - t) * 1000)
            g = grades.get(q["id"])
            entry = score_query(q, res, g)
            entry["top"] = [{"doc_id": r["doc_id"], "title": r["title"][:80], "price": r["price"],
                             "relevance": r.get("relevance"), "grade": int(g.get(r["doc_id"], 0)) if g is not None else None}
                            for r in res[:5]]
            if mode == "hybrid":
                entry["labels"] = label_stats(q, res, g)
            per_query[q["id"]][mode] = entry
        ki = [per_query[q["id"]][mode] for q in queries if q["type"] == "known_item" and not q.get("descriptive")]
        kd = [per_query[q["id"]][mode] for q in queries if q["type"] == "known_item" and q.get("descriptive")]
        at = [per_query[q["id"]][mode] for q in queries if q["type"] == "attribute"]
        summary[mode] = {"known_item_hit1": mean(e["hit1"] for e in ki), "known_item_hit5": mean(e["hit5"] for e in ki),
                         "known_item_mrr": mean(e["mrr"] for e in ki),
                         "described_hit1": mean(e["hit1"] for e in kd), "described_hit5": mean(e["hit5"] for e in kd),
                         "described_mrr": mean(e["mrr"] for e in kd), "attribute_p5": mean(e["p5"] for e in at),
                         "attribute_ndcg10": mean(e["ndcg10"] for e in at), "latency_ms_mean": mean(lat)}

    # Paired comparison on the same attribute queries: averages over 24 queries hide how many queries moved.
    paired = {}
    if "hybrid" in modes:
        at_ids = [q["id"] for q in queries if q["type"] == "attribute"]
        for other in (m for m in modes if m != "hybrid"):
            d = [per_query[i]["hybrid"]["ndcg10"] - per_query[i][other]["ndcg10"] for i in at_ids]
            paired[other] = {"hybrid_better": sum(x > 0.005 for x in d), "hybrid_worse": sum(x < -0.005 for x in d),
                             "tied": sum(abs(x) <= 0.005 for x in d),
                             "worse_on": [i for i, x in zip(at_ids, d) if x < -0.005]}

    labels = None
    if "hybrid" in modes:
        L = {qid: e["hybrid"]["labels"] for qid, e in per_query.items() if "hybrid" in e}
        at = [v for qid, v in L.items() if per_query[qid]["type"] == "attribute"]
        ki = [v for qid, v in L.items() if per_query[qid]["type"] == "known_item"]
        nm = [v for qid, v in L.items() if per_query[qid]["type"] == "no_match"]
        s_rel, s_irr, w_rel = (sum(v[k] for v in at) for k in ("strong_rel", "strong_irrel", "weak_rel"))
        labels = {
            "strong_precision_top5": s_rel / (s_rel + s_irr) if s_rel + s_irr else None,
            "strong_recall_top5": s_rel / (s_rel + w_rel) if s_rel + w_rel else None,
            "attribute_missed_match": [qid for qid, v in L.items() if per_query[qid]["type"] == "attribute" and v["missed_match"]],
            "known_item_found_but_weak": [qid for qid, v in L.items()
                                          if per_query[qid]["type"] == "known_item" and v["target_in_top5"] and not v["target_strong"]],
            "no_match_false_match": [qid for qid, v in L.items() if per_query[qid]["type"] == "no_match" and v["false_match"]],
            "counts": {"attribute": len(at), "known_item": len(ki), "no_match": len(nm)},
        }

    from voice_agent.config import get_settings
    s = get_settings()
    return {"timestamp": datetime.now().isoformat(timespec="seconds"), "top_k": TOP_K,
            "embedding": s.embedding_provider, "catalog_size": len(store.df), "index_load_s": round(load_s, 1),
            "n_queries": {"known_item": sum(q["type"] == "known_item" and not q.get("descriptive") for q in queries),
                          "described": sum(bool(q.get("descriptive")) for q in queries),
                          **{t: sum(q["type"] == t for q in queries) for t in ("attribute", "no_match")}},
            "summary": summary, "paired_vs_hybrid": paired, "labels": labels, "per_query": per_query}


def to_markdown(rep: dict) -> str:
    n = rep["n_queries"]
    lines = [f"# Retrieval benchmark ({rep['timestamp']})", "",
             f"{n['known_item']} named and {n['described']} described known-item, {n['attribute']} attribute and "
             f"{n['no_match']} no-match queries over "
             f"{rep['catalog_size']:,} products; top {rep['top_k']}; embeddings `{rep['embedding']}`. "
             "Generated by `eval/run_retrieval_eval.py`.", "",
             "| Mode | Named item Hit@1 | Hit@5 | MRR | Described item Hit@1 | Hit@5 | MRR | Attribute P@5 | nDCG@10 | ms |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for mode, m in rep["summary"].items():
        lines.append(f"| `{mode}` | {m['known_item_hit1']:.2f} | {m['known_item_hit5']:.2f} | {m['known_item_mrr']:.3f} "
                     f"| {m['described_hit1']:.2f} | {m['described_hit5']:.2f} | {m['described_mrr']:.3f} "
                     f"| {m['attribute_p5']:.2f} | {m['attribute_ndcg10']:.3f} | {m['latency_ms_mean']:.0f} |")
    if rep.get("paired_vs_hybrid"):
        lines += ["", "Paired, per attribute query (nDCG@10 of `hybrid` vs the other mode; ties within 0.005):", "",
                  "| vs | hybrid better | worse | tied | worse on |", "|---|---|---|---|---|"]
        for other, c in rep["paired_vs_hybrid"].items():
            lines.append(f"| `{other}` | {c['hybrid_better']} | {c['hybrid_worse']} | {c['tied']} | "
                         f"{', '.join(f'`{q}`' for q in c['worse_on'])} |")
    L = rep.get("labels")
    if L:
        lines += ["", "## Relevance labels (`hybrid`, what the agent sees)", "",
                  f"- `strong` precision in the top 5 of attribute queries: **{L['strong_precision_top5']:.2f}**",
                  f"- `strong` recall in the top 5 of attribute queries: **{L['strong_recall_top5']:.2f}**",
                  f"- Attribute queries where a relevant product was retrieved but none was labelled strong (agent would "
                  f"say \"not found\"): {len(L['attribute_missed_match'])}/{L['counts']['attribute']} "
                  f"{', '.join(f'`{q}`' for q in L['attribute_missed_match'])}",
                  f"- Known items found in the top 5 but labelled weak: {len(L['known_item_found_but_weak'])}/"
                  f"{L['counts']['known_item']} {', '.join(f'`{q}`' for q in L['known_item_found_but_weak'])}",
                  f"- No-match queries with a `strong` hit (false match): {len(L['no_match_false_match'])}/"
                  f"{L['counts']['no_match']} {', '.join(f'`{q}`' for q in L['no_match_false_match'])}"]
    lines += ["", "## Per query (`hybrid`)", "", "| Query | Type | Result |", "|---|---|---|"]
    for qid, e in rep["per_query"].items():
        h = e.get("hybrid")
        if not h:
            continue
        if e["type"] == "known_item":
            res = f"rank {h['rank']}" if h["rank"] else "not in top 10"
            res += "" if not h["rank"] or h["rank"] > 5 else (" (strong)" if h["labels"]["target_strong"] else " (weak)")
        elif e["type"] == "attribute":
            res = f"P@5 {h['p5']:.1f}, nDCG {h['ndcg10']:.2f} (pool {h['pool']})"
        else:
            res = "false match: " + "; ".join(h["labels"]["strong_titles"]) if h["labels"]["false_match"] else "no strong hit"
        lines.append(f"| `{qid}` | {e['type']} | {res} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--queries", default=str(ROOT / "eval/retrieval/queries.yaml"))
    ap.add_argument("--out", default=str(ROOT / "eval/results/retrieval_eval"))
    ap.add_argument("--show", help="print the top 10 for one query id (per mode) and exit")
    ap.add_argument("--audit", action="store_true", help="list top-5 products the judge graded 0, for review")
    args = ap.parse_args()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    if args.audit:
        # Every product any mode put in its top 5 that the judge graded 0, per attribute query: read these to
        # catch criteria that are too strict (a relevant product graded 0 would penalise whichever mode found it).
        from voice_agent.rag.store import ProductStore
        qs = yaml.safe_load(Path(args.queries).read_text(encoding="utf-8"))["queries"]
        store = ProductStore()
        judge = Judge(store)
        for q in (q for q in qs if q["type"] == "attribute"):
            g = judge.grades(q)
            filters = {k: v for k, v in (q.get("filters") or {}).items() if k in FILTER_KEYS}
            seen: dict[str, tuple[str, set]] = {}
            for mode in modes:
                for r in store.search(q["query"], top_k=5, mode=mode, **filters):
                    if g.get(r["doc_id"], 0) == 0:
                        seen.setdefault(r["doc_id"], (r["title"][:90], set()))[1].add(mode)
            print(f"\n## {q['id']}: {q['query']}  ({len(seen)} graded 0)")
            for title, ms in seen.values():
                print(f"   - {title}   [{','.join(sorted(ms))}]")
        return

    if args.show:
        from voice_agent.rag.store import ProductStore
        qs = {q["id"]: q for q in yaml.safe_load(Path(args.queries).read_text(encoding="utf-8"))["queries"]}
        q, store = qs[args.show], ProductStore()
        g = Judge(store).grades(q) if q["type"] == "attribute" else None
        filters = {k: v for k, v in (q.get("filters") or {}).items() if k in FILTER_KEYS}
        for mode in modes:
            print(f"\n[{mode}] {q['query']}  {filters or ''}")
            for i, r in enumerate(store.search(q["query"], top_k=TOP_K, mode=mode, **filters), 1):
                mark = (g.get(r["doc_id"], 0) if g is not None else ("T" if r["doc_id"] in q.get("targets", []) else "-"))
                print(f"  {i:2d}. [{mark}] {r['relevance']:6s} sim={r['similarity']} ${r['price']:<7} {r['title'][:80]}")
        return

    rep = run(modes, Path(args.queries))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    md = to_markdown(rep)
    out.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md.split("## Per query")[0])
    print(f"Saved {out.with_suffix('.json')} and {out.with_suffix('.md')}")


if __name__ == "__main__":
    main()
