<#
.SYNOPSIS
    Quita el arranque automatico de WISPI y para la instancia en marcha.
#>
[CmdletBinding()]
param([string]$TaskName = "WISPI")

$ErrorActionPreference = "Continue"

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Host "Tarea detenida."
} catch {
    Write-Host "La tarea no estaba corriendo."
}

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Tarea '$TaskName' eliminada." -ForegroundColor Green
} catch {
    Write-Host "No habia ninguna tarea '$TaskName' registrada."
}

# El pythonw huerfano no muere con la tarea: hay que buscarlo por linea de
# comando, porque puede haber otros pythonw del usuario que NO son WISPI.
$procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
         Where-Object { $_.CommandLine -like "*wispi*" }
if ($procs) {
    $procs | ForEach-Object {
        Write-Host "Matando pythonw huerfano PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "No hay procesos de WISPI en marcha."
}
