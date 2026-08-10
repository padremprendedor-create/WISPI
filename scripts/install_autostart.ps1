<#
.SYNOPSIS
    Registra WISPI para que arranque al iniciar sesion en Windows.

.DESCRIPTION
    Crea una tarea programada que lanza WISPI con pythonw.exe (sin ventana de
    consola) al hacer logon.

    POR QUE TAREA PROGRAMADA Y NO LA CARPETA DE INICIO
    La carpeta Startup no permite elegir el nivel de privilegios ni reintentar
    si falla, y muestra la ventana un instante. Task Scheduler hace las tres
    cosas bien.

.PARAMETER Elevated
    OPT-IN EXPLICITO. Registra la tarea con privilegios de administrador.

    QUE GANAS: el hook de teclado tambien funciona cuando tienes en primer plano
    una ventana elevada (Administrador de tareas, una terminal de admin). Sin
    esto, UIPI bloquea el input de procesos bajos hacia ventanas altas y WISPI
    parece "no funcionar" ahi.

    QUE ACEPTAS: un hook de teclado global corriendo como administrador tiene la
    misma forma tecnica que un keylogger. La unica contramedida real es que el
    codigo no escriba lo que ve: wispi/logging_setup.py nunca registra vkCode
    fuera del combo, y logging.include_text esta en false por defecto. Si activas
    esto, esa promesa es lo unico que te protege. Leela antes.

    El default es SIN elevar, a proposito.

.EXAMPLE
    .\scripts\install_autostart.ps1
    .\scripts\install_autostart.ps1 -Elevated
#>
[CmdletBinding()]
param(
    [switch]$Elevated,
    [string]$TaskName = "WISPI"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    Write-Error "No encuentro $pythonw. Corre primero: uv sync (desde $root)"
    exit 1
}

Write-Host "WISPI    : $root"
Write-Host "Ejecuta  : $pythonw -m wispi"
Write-Host "Elevado  : $(if ($Elevated) { 'SI (opt-in explicito)' } else { 'no (default)' })"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m wispi" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ExecutionTimeLimit 0 = sin limite. WISPI es un demonio: si el limite queda al
# default de 3 dias, Windows lo mata en silencio y el usuario no sabe por que
# dejo de funcionar.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$runLevel = if ($Elevated) { "Highest" } else { "Limited" }
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel $runLevel

try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "WISPI - dictado por voz local. Arranca al logon." | Out-Null

Write-Host ""
Write-Host "Tarea '$TaskName' registrada." -ForegroundColor Green
Write-Host "Arrancar ahora sin reiniciar:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Quitar:                        .\scripts\uninstall_autostart.ps1"
Write-Host ""
Write-Host "PENDIENTE DE VERIFICACION HUMANA (criterio C10.1 del SPEC):" -ForegroundColor Yellow
Write-Host "  reinicia Windows y comprueba que el icono aparece en la bandeja SIN"
Write-Host "  ventana de consola, y que el primer dictado funciona sin tocar nada."
