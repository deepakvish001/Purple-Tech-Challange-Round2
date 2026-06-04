<#
.SYNOPSIS
  Run repository tests in PowerShell.

.DESCRIPTION
  Wrapper around pytest with convenient unit/integration/all options.
#>

param(
  [string]$Mode = 'all'
)

switch ($Mode) {
  'unit' {
    pytest tests/unit -q
    break
  }
  'integration' {
    pytest tests/integration -q
    break
  }
  'all' {
    pytest -q
    break
  }
  default {
    Write-Host "Unknown mode: $Mode" -ForegroundColor Red
    Write-Host "Usage: .\scripts\run_tests.ps1 [-Mode unit|integration|all]"
    exit 2
  }
}
