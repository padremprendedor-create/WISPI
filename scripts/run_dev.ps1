<#
.SYNOPSIS
    Arranca WISPI en modo desarrollo: consola visible, log en DEBUG.
#>
[CmdletBinding()]
param([string]$LogLevel = "DEBUG")

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "WISPI dev | $root | log=$LogLevel" -ForegroundColor Cyan
Write-Host "Manten Ctrl+Win para dictar. Ctrl+C para salir." -ForegroundColor Cyan
uv run python -m wispi --console --log-level $LogLevel
