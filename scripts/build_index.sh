#!/usr/bin/env bash
# Download the Household Supplies slice of Amazon Reviews 2023 (if needed), build products.parquet, then the vector index.
set -euo pipefail
[ -f data/raw/hh2023/meta_household_supplies.jsonl ] || python -m voice_agent.data.download_hh2023
python -m voice_agent.data.ingest
python -m voice_agent.rag.store --build
