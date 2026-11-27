"""Retrieval ablation modes, relevance labels and the benchmark's relevance judge (household catalog).
Skipped if the index has not been built."""
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
    q = dict(query="dish soap", top_k=5, max_price=5)
    assert [r["doc_id"] for r in store.search(**q)] == [r["doc_id"] for r in store.search(**q, mode="hybrid")]


@pytest.mark.parametrize("mode", ["dense", "bm25", "hybrid_rrf", "hybrid"])
def test_every_mode_respects_filters_and_labels_results(store, mode):
    res = store.search("cleaner", top_k=5, max_price=5, brand="Affresh", mode=mode)
    assert res and all(r["price"] <= 5 and "affresh" in r["title"].lower() for r in res)
    assert all(r["relevance"] in ("strong", "weak") for r in res)


def test_unknown_mode_is_rejected(store):
    with pytest.raises(ValueError):
        store.search("sponge", mode="cross_encoder")


def test_relevance_labels_separate_matches_from_neighbours(store):
    # The catalog has no robot vacuums; vacuum brushes and vacuum-mop pads are only neighbours.
    assert not [r for r in store.search("robot vacuum", top_k=5, max_price=200) if r["relevance"] == "strong"]
    # Brand filter + the requested type in the title is a match even when the similarity of "cleaner" is low.
    affresh = store.search("cleaner", top_k=5, max_price=5, brand="Affresh")
    assert {r["title"].split(",")[0] for r in affresh} == {"Affresh Cooktop Cleaner", "Affresh Stainless Steel Cleaning Spray"}
    assert all(r["relevance"] == "strong" for r in affresh)
    # Real matches stay strong.
    for q, f, word in [("dish soap", {"max_price": 5}, "dish"),
                       ("eco-friendly stainless steel cleaner", {"max_price": 15}, "stainless steel"),
                       ("fragrance free laundry detergent", {}, "laundry detergent")]:
        top = store.search(q, top_k=5, **f)
        assert all(r["relevance"] == "strong" and word in r["title"].lower() for r in top), q


def test_judge_grades_from_criteria_not_from_retrievers(store, bench):
    judge = bench.Judge(store)
    affresh = judge.grades({"relevant": {"title": ["clean"]}, "filters": {"max_price": 5, "brand": "Affresh"}})
    assert int((affresh > 0).sum()) == 2          # exactly two Affresh products are <= $5 in the catalog, both cleaners
    # `any` is an OR: a Clorox wipe whose title omits "disinfecting" still counts through its category
    wipes = judge.grades({"relevant": {"any": [{"category": ["Disinfectant Wipes"]},
                                               {"title": ["disinfect"]}]}})
    lemon = store.df.index[store.df["title"] == "Clorox, Crisp Lemon, 75 Count"][0]
    assert wipes[lemon] > 0
    # `all` is an AND of sub-criteria: a trash bag must also be 13 gallon
    bags = judge.grades({"relevant": {"all": [{"title": ["trash bags?"]}, {"title": ["13[- ]?gal"]}]}})
    titles = store.df["title"].str.lower()
    assert bags[titles.str.contains("13 gallon") & titles.str.contains("trash bag")].min() > 0
    assert bags[titles.str.contains(r"\b4 gallon") & titles.str.contains("trash bag")].max() == 0
    # `ideal` adds grade 2 on top of `relevant`; exclude_title removes holders and dispensers
    g = judge.grades({"relevant": {"title": ["dryer sheets?"]}, "ideal": {"text": ["unscented"]},
                      "exclude_title": ["dispenser", "holder"]})
    assert set(g.unique()) == {0, 1, 2}
    assert g[titles.str.contains("dryer sheet dispenser")].max() == 0


def test_benchmark_metrics(bench):
    q = {"type": "known_item", "targets": ["b"]}
    assert bench.score_query(q, [{"doc_id": "a"}, {"doc_id": "b"}], None) == {"hit1": 0.0, "hit5": 1.0, "mrr": 0.5, "rank": 2}
    import pandas as pd
    grades = pd.Series({"a": 2, "b": 0, "c": 2})              # 2 relevant in the catalog -> P@5 is over the top 2
    m = bench.score_query({"type": "attribute"}, [{"doc_id": "a"}, {"doc_id": "b"}, {"doc_id": "c"}], grades)
    assert m["p5"] == 0.5 and m["pool"] == 2 and 0 < m["ndcg10"] < 1


def test_benchmark_file_is_consistent(store):
    import yaml
    qs = yaml.safe_load((ROOT / "eval/retrieval/queries.yaml").read_text(encoding="utf-8"))["queries"]
    assert len({q["id"] for q in qs}) == len(qs)
    assert {q["type"] for q in qs} == {"attribute", "known_item", "no_match"}
    for q in qs:
        assert all(t in store.df.index for t in q.get("targets", [])), q["id"]
