<#
.SYNOPSIS
    Crea un acceso directo de WISPI en el Escritorio (sin consola visible).

.DESCRIPTION
    Apunta a pythonw.exe del venv del proyecto con "-m wispi", con el
    directorio de trabajo en la raiz del repo (igual que la tarea programada
    de autostart, pero para lanzarlo a demanda con doble clic).
#>
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path ([Environment]::GetFolderPath('Desktop')) "WISPI.lnk")
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$icon = Join-Path $root "assets\icon.ico"

if (-not (Test-Path $pythonw)) {
    Write-Error "No encuentro $pythonw. Corre primero: uv sync (desde $root)"
    exit 1
}
if (-not (Test-Path $icon)) {
    Write-Error "No encuentro $icon. Corre primero: .venv\Scripts\python.exe scripts\generate_icon.py"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Destination)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "-m wispi"
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "WISPI - dictado por voz local"
$shortcut.Save()

Write-Host "Acceso directo creado: $Destination" -ForegroundColor Green
