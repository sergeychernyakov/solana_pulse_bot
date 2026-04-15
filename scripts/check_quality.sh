# scripts/check_quality.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
PYLINT_HOME="${PYLINT_HOME:-${ROOT_DIR}/.cache/pylint}"

cd "${ROOT_DIR}"
mkdir -p "${PYLINT_HOME}" tmp

run() {
  local title="$1"
  shift
  printf '\n==> %s\n' "${title}"
  "$@"
}

require_module() {
  local module="$1"
  "${PYTHON_BIN}" -c "import ${module}" >/dev/null 2>&1 || {
    printf 'Missing Python module: %s\n' "${module}" >&2
    printf 'Install dev dependencies with: %s -m pip install -r requirements.txt\n' "${PYTHON_BIN}" >&2
    exit 1
  }
}

require_module black
require_module isort
require_module mypy
require_module pylint
require_module pytest
require_module ruff

run "Bash syntax check" \
  bash -n scripts/check_quality.sh

run "Python syntax compile" \
  "${PYTHON_BIN}" -m compileall -q main.py run_api.py src pulse_bot tests

run "Ruff lint" \
  "${PYTHON_BIN}" -m ruff check .

run "Black format check" \
  "${PYTHON_BIN}" -m black --line-length 120 --check .

run "isort import check" \
  "${PYTHON_BIN}" -m isort --profile black --line-length 120 --check-only .

run "mypy type check" \
  "${PYTHON_BIN}" -m mypy src main.py pulse_bot

run "pylint static analysis" \
  env PYLINTHOME="${PYLINT_HOME}" "${PYTHON_BIN}" -m pylint --rcfile .pylintrc --fail-under=10.0 src main.py

rm -f .coverage
run "pytest with coverage" \
  "${PYTHON_BIN}" -m pytest -q --cov=src --cov-report=term-missing --cov-fail-under=90

if "${PYTHON_BIN}" -c "import bandit" >/dev/null 2>&1; then
  run "Bandit security scan" \
    "${PYTHON_BIN}" -m bandit -q -r src pulse_bot main.py run_api.py -x tests
else
  printf '\n==> Bandit security scan\nSkipping: bandit is not installed.\n'
fi

if "${PYTHON_BIN}" -c "import pip_audit" >/dev/null 2>&1; then
  run "pip-audit dependency audit" \
    "${PYTHON_BIN}" -m pip_audit --local --progress-spinner off
else
  printf '\n==> pip-audit dependency audit\nSkipping: pip-audit is not installed.\n'
fi

if "${PYTHON_BIN}" -c "import pre_commit" >/dev/null 2>&1; then
  run "pre-commit hooks" \
    "${PYTHON_BIN}" -m pre_commit run --all-files
else
  printf '\n==> pre-commit hooks\nSkipping: pre-commit is not installed.\n'
fi

printf '\nAll configured quality checks completed.\n'
