#!/usr/bin/env bash
# Start the Streamlit UI at http://localhost:8501
exec python -m streamlit run src/voice_agent/ui/app.py --server.port 8501
