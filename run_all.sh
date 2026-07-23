#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ and rerun." >&2
  exit 1
fi

uv sync --frozen --project "$PROJECT_DIR"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

"$PYTHON_BIN" "$PROJECT_DIR/scripts/check_inputs.py"
"$PYTHON_BIN" "$PROJECT_DIR/scripts/run_final_analysis.py"
"$PYTHON_BIN" "$PROJECT_DIR/scripts/run_oasis2_replication.py" --download
"$PYTHON_BIN" "$PROJECT_DIR/scripts/build_publication.py"

echo "Final outputs: $PROJECT_DIR/final_report_outputs"
echo "Publication files: $PROJECT_DIR/publication"
