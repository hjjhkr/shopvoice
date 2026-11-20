# Download the Kaggle dataset (if needed), build products.parquet, then build the vector index.
python -m voice_agent.data.ingest
python -m voice_agent.rag.store --build
