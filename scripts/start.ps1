<#
.SYNOPSIS
  Start the full stack for local development (PowerShell / Windows)

.DESCRIPTION
  Wrapper around `docker compose up` with named profiles. Run from the
  repository root.
#>

param(
  [string]$Profile = 'default'
)

switch ($Profile) {
  'default' {
    docker compose up --build
    break
  }
  'video' {
    docker compose --profile video up --build
    break
  }
  'full' {
    docker compose --profile full up --build
    break
  }
  default {
    Write-Host "Unknown profile: $Profile" -ForegroundColor Red
    Write-Host "Usage: .\scripts\start.ps1 [-Profile default|video|full]"
    exit 2
  }
}
