"""Download the Household Supplies slice of Amazon Reviews 2023 (McAuley Lab), Health & Household metadata.

The full category metadata is a 2.5 GB JSONL file. It is streamed once and only the products this assistant can
use are kept: the "Household Supplies" sub-tree, with a numeric price, a title and at least MIN_RATINGS ratings.
The result (a few hundred MB) is written to data/raw/hh2023/meta_household_supplies.jsonl.

Source: Hou et al., "Bridging Language and Items for Retrieval and Recommendation", arXiv:2403.03952, 2024.
https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023

    python -m voice_agent.data.download_hh2023            # about 5-15 minutes depending on the connection
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

META_URL = ("https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/"
            "raw/meta_categories/meta_Health_and_Household.jsonl")
SUBTREE = "Household Supplies"
MIN_RATINGS = 10


def keep(item: dict) -> bool:
    cats = item.get("categories") or []
    return (len(cats) > 1 and cats[1] == SUBTREE and isinstance(item.get("price"), (int, float)) and item["price"] > 0
            and bool((item.get("title") or "").strip()) and (item.get("rating_number") or 0) >= MIN_RATINGS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw/hh2023/meta_household_supplies.jsonl")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    seen = kept = 0
    t0 = time.time()
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as client, \
            client.stream("GET", META_URL) as r, tmp.open("w", encoding="utf-8") as f:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        for line in r.iter_lines():
            if not line:
                continue
            seen += 1
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if keep(item):
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
                kept += 1
            if seen % 100_000 == 0:
                done = r.num_bytes_downloaded / total if total else 0
                print(f"{seen:,} read, {kept:,} kept, {done:.0%} of {total / 1e9:.2f} GB, {time.time() - t0:.0f}s", flush=True)
    tmp.replace(out)
    print(f"done: {seen:,} products read, {kept:,} kept -> {out}", flush=True)
    (out.parent / "download_stats.json").write_text(json.dumps(
        {"source": META_URL, "subtree": SUBTREE, "min_ratings": MIN_RATINGS, "read": seen, "kept": kept}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
