#!/usr/bin/env bash
# Tests, stage-level checks (text + voice), then the ProofAgent evaluation.
set -euo pipefail
python -m pytest
python eval/run_stage_checks.py
python eval/run_stage_checks.py --voice
python eval/run_proofagent.py
