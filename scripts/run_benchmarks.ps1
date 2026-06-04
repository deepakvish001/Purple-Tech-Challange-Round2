param(
  [string]$SaveName = ''
)

if (-not (Get-Command pytest -ErrorAction SilentlyContinue)) {
  Write-Error "pytest not found; ensure your virtualenv is active and pytest is installed"
  exit 2
}

try {
  python - <<'PY'
import pytest_benchmark
print('pytest-benchmark available')
PY
} catch {
  Write-Error "pytest-benchmark not installed. Install with: pip install pytest-benchmark"
  exit 2
}

if ($SaveName) {
  pytest tests/benchmarks -q --benchmark-save=$SaveName
} else {
  pytest tests/benchmarks -q
}
