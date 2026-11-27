"""Hybrid retrieval over the private catalog.

    dense  : Chroma vector search (cosine) over title + features + description
    sparse : BM25 over the same text
    filter : metadata constraints (price / brand / category / rating) applied to BOTH
    fuse   : reciprocal rank fusion, then a light constraint-aware rerank

Build the index with:  python -m voice_agent.rag.store --build
"""
from __future__ import annotations

import argparse
import math
import re
import time
from typing import Any

import pandas as pd

from ..config import Settings, get_settings

SEARCH_MODES = ("hybrid", "hybrid_rrf", "dense", "bm25")
SMALL_POOL = 1500       # at or below this many products left by the filters, dense similarity is computed for all of them
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an the and or for with of to in on at by from is are be it this that i me my need want "
            "looking find recommend show get buy some any good best please under below over than less more "
            "dollars dollar usd cheap price priced around about".split())


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(str(text).lower()) if t not in _STOP and len(t) > 1]


def norm_brand(text: str) -> str:
    """Case-, punctuation- and '&'/'and'-insensitive form used for brand matching."""
    text = str(text).lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def doc_text(row: pd.Series | dict) -> str:
    """The text that is embedded for one product."""
    parts = [row["title"], row.get("features") or "", row.get("description") or ""]
    if row.get("brand"):
        parts.append(f"Brand: {row['brand']}")
    if row.get("category"):
        parts.append(f"Category: {row['category']}")
    for key, label in (("claims", "Listing claims"), ("ingredients", "Ingredients or materials"),
                       ("recommended_age", "Recommended age")):
        if isinstance(row.get(key), str) and row[key]:
            parts.append(f"{label}: {row[key]}")
    return " \n".join(p for p in parts if p)[:2000]


def embedding_function(settings: Settings):
    """Swappable embeddings: local ONNX MiniLM (default), Ollama, or OpenAI."""
    from chromadb.utils import embedding_functions as ef

    provider = settings.embedding_provider
    if provider == "openai":
        import os
        return ef.OpenAIEmbeddingFunction(
            api_key=os.environ["OPENAI_API_KEY"],
            model_name=settings.embedding_model or "text-embedding-3-small",
        )
    if provider == "ollama":
        return ef.OllamaEmbeddingFunction(
            url=(settings.llm_base_url or "http://localhost:11434"),
            model_name=settings.embedding_model or "nomic-embed-text",
        )
    return ef.DefaultEmbeddingFunction()   # all-MiniLM-L6-v2 via onnxruntime, no API key


def _client(settings: Settings):
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    settings.chroma_dir.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(settings.chroma_dir), settings=ChromaSettings(anonymized_telemetry=False)
    )


def _num(v) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def build_index(settings: Settings | None = None, batch: int = 256, limit: int | None = None) -> int:
    settings = settings or get_settings()
    df = pd.read_parquet(settings.products_parquet)
    if limit:
        df = df.head(limit)
    client = _client(settings)
    try:
        client.delete_collection(settings.collection)
    except Exception:
        pass
    col = client.create_collection(
        settings.collection,
        embedding_function=embedding_function(settings),
        metadata={"hnsw:space": "cosine", "embedding_provider": settings.embedding_provider},
    )
    t0 = time.time()
    for start in range(0, len(df), batch):
        chunk = df.iloc[start:start + batch]
        metadatas = []
        for _, r in chunk.iterrows():
            meta: dict[str, Any] = {"price": float(r["price"]), "top_category": r["top_category"] or ""}
            for key in ("brand", "sku"):
                if isinstance(r[key], str) and r[key]:
                    meta[key] = r[key]
            rating = _num(r["rating"])
            if rating is not None:
                meta["rating"] = rating
                meta["rating_count"] = int(r.get("rating_count") or 0)
            metadatas.append(meta)
        col.add(
            ids=chunk["doc_id"].tolist(),
            documents=[doc_text(r) for _, r in chunk.iterrows()],
            metadatas=metadatas,
        )
        print(f"  indexed {min(start + batch, len(df)):,}/{len(df):,}  ({time.time() - t0:.0f}s)", flush=True)
    return len(df)


class ProductStore:
    """Read side of the index. Loaded once per process (the MCP server owns it)."""

    def __init__(self, settings: Settings | None = None):
        from rank_bm25 import BM25Okapi

        self.settings = settings or get_settings()
        self.df = pd.read_parquet(self.settings.products_parquet).set_index("doc_id", drop=False)
        self._cat_title = (self.df["category"].fillna("") + " " + self.df["title"]).str.lower()
        self._brand_norm = self.df["brand"].fillna("").map(norm_brand)
        self._title_norm = self.df["title"].map(lambda t: f" {norm_brand(t)} ")
        self._ids = self.df["doc_id"].tolist()
        self._pos = {d: i for i, d in enumerate(self._ids)}
        self._bm25 = BM25Okapi([tokenize(doc_text(r)) for _, r in self.df.iterrows()])
        self._ef = embedding_function(self.settings)
        self._col = _client(self.settings).get_collection(self.settings.collection, embedding_function=self._ef)

    def _dense_within(self, query: str, ids: list[str]) -> list[tuple[str, float]]:
        """Cosine similarity of the query to each of `ids`, highest first (same embeddings as the index)."""
        import numpy as np

        if not ids:
            return []
        q = np.asarray(self._ef([query])[0], dtype="float32")
        got = self._col.get(ids=ids, include=["embeddings"])
        emb = np.asarray(got["embeddings"], dtype="float32")
        sims = emb @ q / (np.linalg.norm(emb, axis=1) * np.linalg.norm(q) + 1e-12)
        return sorted(zip(got["ids"], (float(s) for s in sims)), key=lambda x: -x[1])

    # ---- metadata filtering -------------------------------------------------
    def _mask(self, max_price, min_price, brand, category, min_rating) -> pd.Series:
        df = self.df
        mask = pd.Series(True, index=df.index)
        if max_price is not None:
            mask &= df["price"] <= float(max_price)
        if min_price is not None:
            mask &= df["price"] >= float(min_price)
        if brand:
            b = norm_brand(brand)      # "Arm and Hammer" (ASR) == "Arm & Hammer" (catalog)
            if b:   # manufacturer names recovered from the spec text may have lost their spaces, so compare both ways
                mask &= (self._brand_norm.str.contains(b, regex=False)
                         | self._brand_norm.str.replace(" ", "").str.contains(b.replace(" ", ""), regex=False)
                         | self._title_norm.str.contains(f" {b} ", regex=False))
        if category:
            # Users say "dish soap"; the catalog says "... > Dishwashing > Dishwashing Liquid". Every word
            # of the requested category must occur in the category path or the title (singular/plural-insensitive).
            for token in tokenize(category):
                stem = token[:-1] if len(token) > 3 and token.endswith("s") else token
                mask &= self._cat_title.str.contains(stem, regex=False)
        if min_rating is not None:
            mask &= df["rating"].fillna(-1) >= float(min_rating)   # unrated products cannot satisfy a rating floor
        return mask

    # ---- hybrid search ------------------------------------------------------
    def search(
        self,
        query: str,
        top_k: int = 5,
        max_price: float | None = None,
        min_price: float | None = None,
        brand: str | None = None,
        category: str | None = None,
        min_rating: float | None = None,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """`mode` selects the ranking, for the retrieval ablation (eval/run_retrieval_eval.py):
        hybrid (default, what the agent uses) = RRF of dense + BM25, then the title-coverage rerank;
        hybrid_rrf = RRF only; dense = vector similarity only; bm25 = BM25 only.
        Filters and relevance labels are the same in every mode."""
        if mode not in SEARCH_MODES:
            raise ValueError(f"mode must be one of {SEARCH_MODES}, got {mode!r}")
        top_k = max(1, min(int(top_k), 20))
        mask = self._mask(max_price, min_price, brand, category, min_rating)
        allowed = set(self.df.index[mask])
        if not allowed:
            return []

        # dense: numeric filters are pushed down into Chroma; text filters are post-applied
        where_parts = []
        if max_price is not None:
            where_parts.append({"price": {"$lte": float(max_price)}})
        if min_price is not None:
            where_parts.append({"price": {"$gte": float(min_price)}})
        where = None if not where_parts else where_parts[0] if len(where_parts) == 1 else {"$and": where_parts}
        n = min(len(self._ids), max(60, top_k * 12))
        if len(allowed) <= SMALL_POOL:
            # The filters leave few products (a brand, a rating floor, a tight budget). The global top-n of a
            # vector query can miss all of them, so every allowed product is scored directly instead.
            dense = self._dense_within(query, sorted(allowed))[:n]
        else:
            res = self._col.query(query_texts=[query], n_results=n, where=where, include=["distances"])
            dense = [(d, 1.0 - dist) for d, dist in zip(res["ids"][0], res["distances"][0]) if d in allowed]

        # sparse
        q_tokens = tokenize(query)
        scores = self._bm25.get_scores(q_tokens) if q_tokens else []
        sparse = sorted(
            ((d, float(scores[self._pos[d]])) for d in allowed if len(scores) and scores[self._pos[d]] > 0),
            key=lambda x: -x[1],
        )[:n]

        # reciprocal rank fusion
        fused: dict[str, float] = {}
        for ranked in (dense, sparse):
            for rank, (d, _) in enumerate(ranked):
                fused[d] = fused.get(d, 0.0) + 1.0 / (60 + rank + 1)
        dense_sim = dict(dense)

        # rerank: reward query-term coverage in the title (precision on product type)
        def rerank(d: str) -> float:
            title_tokens = set(tokenize(self.df.at[d, "title"]))
            coverage = (sum(t in title_tokens for t in q_tokens) / len(q_tokens)) if q_tokens else 0.0
            return fused[d] * (1.0 + 0.6 * coverage)

        if mode == "dense":
            score = dense_sim
        elif mode == "bm25":
            score = dict(sparse)
        elif mode == "hybrid_rrf":
            score = fused
        else:
            score = {d: rerank(d) for d in fused}
        ranked = sorted(score, key=score.get, reverse=True)[:top_k]
        out = []
        for d in ranked:
            item = self._to_result(d, score[d], dense_sim.get(d))
            item["relevance"] = self._relevance(d, q_tokens, dense_sim.get(d), brand_matched=bool(brand))
            out.append(item)
        return out

    def _relevance(self, doc_id: str, q_tokens: list[str], similarity: float | None, brand_matched: bool = False) -> str:
        """'strong' = confidently the kind of product asked for. Hybrid search always returns
        its nearest neighbours, so the agent needs this to tell 'no such product' from a match.
        Thresholds are calibrated for the default MiniLM embeddings (RAG_STRONG_SIM / RAG_WEAK_SIM) on the
        household catalog with eval/run_retrieval_eval.py (no-match false-strong rate, strong precision/recall).

        Similarity says "same neighbourhood", not "same product": a vacuum brush and robot-mop pads sit next to
        "robot vacuum", espresso cups next to "espresso machine". So the words of the request must also be in
        the product. Word matching ignores plural endings (battery/batteries, towel/towels).
            in_title   = share of the query words that are in the title
            in_listing = share of the query words anywhere in the listing (title, features, description,
                         brand, category), so "ant killer" accepts "Multi Insect Killer ... kills ants"
        strong when
            very similar (sim >= strong + 0.10) and at least half the query words are in the listing, or
            similar (sim >= strong) and the title has more than half the query words (for "robot vacuum" a
                title with only "vacuum" is not enough), or some of them and the listing has all, or
            somewhat similar (sim >= weak) and the title has nearly all query words, or
            the user named a brand, the product has it, and it is somewhat similar or its title names
                the requested product type (brand-X "cleaner" -> "Brand-X Cooktop Cleaner", whatever the sim).
        """
        import os
        strong = float(os.environ.get("RAG_STRONG_SIM", "0.45"))
        weak = float(os.environ.get("RAG_WEAK_SIM", "0.35"))
        sim = similarity or 0.0

        def stem(t: str) -> str:              # plural-insensitive: batteries -> battery, towels -> towel
            if len(t) > 4 and t.endswith("ies"):
                return t[:-3] + "y"
            return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t

        def share(words: list[str], vocab: set[str]) -> float:
            return sum(w in vocab for w in words) / len(words) if words else 0.0

        row = self.df.loc[doc_id]
        title = {stem(t) for t in tokenize(row["title"])}
        listing = title | {stem(t) for t in tokenize(doc_text(row))}
        query = [stem(t) for t in q_tokens]
        in_title = share(query, title)
        in_listing = share(query, listing) if query else 1.0
        if sim >= strong + 0.10 and in_listing >= 0.5:
            return "strong"
        if sim >= strong and (in_title > 0.5 or (in_title >= 0.3 and in_listing == 1.0)):
            return "strong"
        if sim >= weak and in_title >= 0.66:
            return "strong"
        if brand_matched:
            # The result already satisfies a brand the user named, which is strong evidence by itself. Without
            # this, "a brand-X product under $5" flipped between match and no-match as the query wording varied.
            # Within one brand the embedding of a one-word query ("cleaner") is a poor guide, so a title that
            # names the requested type also counts.
            brand = {stem(t) for t in tokenize(row["brand"] if isinstance(row["brand"], str) else "")}
            rest = [t for t in query if t not in brand]
            if sim >= weak or (rest and share(rest, title) >= 0.5):
                return "strong"
        return "weak"

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self._to_result(doc_id, None, None) if doc_id in self.df.index else None

    def _to_result(self, doc_id: str, score: float | None, similarity: float | None) -> dict[str, Any]:
        r = self.df.loc[doc_id]
        return {
            "doc_id": doc_id,
            "sku": r["sku"] if isinstance(r["sku"], str) else None,
            "title": r["title"],
            "price": _num(r["price"]),
            "price_max": _num(r["price_max"]),
            "rating": _num(r["rating"]),
            "rating_count": int(r["rating_count"]) if _num(r.get("rating_count")) is not None else None,
            "brand": r["brand"] if isinstance(r["brand"], str) else None,
            "brand_inferred": bool(r["brand_inferred"]),
            "brand_source": r.get("brand_source"),
            "recommended_age": r["recommended_age"] if isinstance(r.get("recommended_age"), str) else None,
            "age_min_years": _num(r.get("age_min_years")),
            "ingredients": r["ingredients"] if isinstance(r["ingredients"], str) else None,
            "claims": r["claims"] if isinstance(r.get("claims"), str) else None,
            "scent": r["scent"] if isinstance(r.get("scent"), str) else None,
            "form": r["form"] if isinstance(r.get("form"), str) else None,
            "size": r["size"] if isinstance(r.get("size"), str) else None,
            "category": r["category"] or None,
            "features": (r["features"] or "")[:500],
            "price_per_oz": _num(r["price_per_oz"]),
            "price_per_count": _num(r.get("price_per_count")),
            "url": r["url"] if isinstance(r["url"], str) else None,
            "score": round(score, 5) if score is not None else None,
            "similarity": round(similarity, 4) if similarity is not None else None,
            "source": r.get("source") or "amazon-reviews-2023/health-and-household",
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build or query the product index")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--query")
    ap.add_argument("--max-price", type=float)
    args = ap.parse_args()
    if args.build:
        n = build_index(limit=args.limit)
        print(f"Indexed {n:,} products into {get_settings().chroma_dir}")
    if args.query:
        for r in ProductStore().search(args.query, max_price=args.max_price):
            print(f"{r['doc_id']}  ${r['price']:<8} {r['title'][:90]}")


if __name__ == "__main__":
    main()
