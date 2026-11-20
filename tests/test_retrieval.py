"""Retrieval ablation modes and the benchmark's relevance judge. Skipped if the index has not been built."""
import importlib.util
from pathlib import Path

import pytest

from voice_agent.config import get_settings

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.products_parquet.exists() or not settings.chroma_dir.exists(),
    reason="index not built (run scripts/build_index)",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def store():
    from voice_agent.rag.store import ProductStore
    return ProductStore()


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("run_retrieval_eval", ROOT / "eval/run_retrieval_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_mode_is_the_production_hybrid(store):
    q = dict(query="wooden puzzle for a toddler", top_k=5, max_price=20)
    assert [r["doc_id"] for r in store.search(**q)] == [r["doc_id"] for r in store.search(**q, mode="hybrid")]


@pytest.mark.parametrize("mode", ["dense", "bm25", "hybrid_rrf", "hybrid"])
def test_every_mode_respects_filters_and_labels_results(store, mode):
    res = store.search("LEGO set", top_k=5, max_price=5, brand="LEGO", mode=mode)
    assert res and all(r["price"] <= 5 and "lego" in r["title"].lower() for r in res)
    assert all(r["relevance"] in ("strong", "weak") for r in res)


def test_unknown_mode_is_rejected(store):
    with pytest.raises(ValueError):
        store.search("puzzle", mode="cross_encoder")


def test_judge_grades_from_criteria_not_from_retrievers(store, bench):
    judge = bench.Judge(store)
    lego = judge.grades({"relevant": {"title": [r"\blego\b"]}, "filters": {"max_price": 5, "brand": "LEGO"}})
    assert int((lego > 0).sum()) == 1                         # exactly one LEGO product is <= $5 in the catalog
    # `any` is an OR: an RC truck whose title lacks "RC" still counts through its category
    rc = judge.grades({"relevant": {"any": [{"title": [r"\brc\b"]},
                                            {"category": ["Controlled Vehicles > (?:Cars|Trucks)"]}]}})
    trophy_rat = store.df.index[store.df["title"].str.startswith("Team Associated 70019")][0]
    assert rc[trophy_rat] > 0
    # `ideal` adds grade 2 on top of `relevant`
    g = judge.grades({"relevant": {"title": [r"\bpuzzle"]}, "ideal": {"text": [r"\bwood"]}})
    assert set(g.unique()) == {0, 1, 2}


def test_benchmark_metrics(bench):
    q = {"type": "known_item", "targets": ["b"]}
    assert bench.score_query(q, [{"doc_id": "a"}, {"doc_id": "b"}], None) == {"hit1": 0.0, "hit5": 1.0, "mrr": 0.5, "rank": 2}
    import pandas as pd
    grades = pd.Series({"a": 2, "b": 0, "c": 2})              # 2 relevant in the catalog -> P@5 is over the top 2
    m = bench.score_query({"type": "attribute"}, [{"doc_id": "a"}, {"doc_id": "b"}, {"doc_id": "c"}], grades)
    assert m["p5"] == 0.5 and m["pool"] == 2 and 0 < m["ndcg10"] < 1
