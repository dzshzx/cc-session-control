#!/usr/bin/env bash
# 本地与 CI 共用的唯一质量门：.github/workflows/quality-gate.yml 只调用本脚本，
# 覆盖率阈值由 scripts/check_coverage.py 的常量持有，这里不重复传参。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

uv run --extra dev ruff check src tests scripts
uv run --extra dev ruff format --check src tests scripts
uv run --extra dev mypy src/
if grep -rn --include='*.py' '/home/' src/; then
  echo 'hardcoded home path under src/ (see above)' >&2
  exit 1
fi
uv run --extra dev pytest tests/ \
  --cov=cc_session_control --cov-branch \
  --cov-report=term-missing --cov-report=json
uv run --extra dev python scripts/check_coverage.py coverage.json
