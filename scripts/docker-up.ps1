# Windows-native equivalent of scripts/docker-up.js — for machines without
# Node/pnpm installed. Wraps `docker compose up`: if API_HOST_PORT (default
# 8000) or FRONTEND_HOST_PORT (default 3000) is already taken by something
# else on this machine, walks forward to the next free port instead of
# failing with "port is already allocated". Pure PowerShell + .NET, no
# extra tooling required.
#
# Usage (from the repo root):
#   .\scripts\docker-up.ps1 -d --build
# or, if PowerShell's execution policy blocks running scripts:
#   .\scripts\docker-up.cmd -d --build

function Test-PortFree {
    param([int]$Port)
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $listener.Start()
        $listener.Stop()
        return $true
    } catch {
        return $false
    }
}

function Find-FreePort {
    param(
        [int]$StartPort,
        [System.Collections.Generic.HashSet[int]]$Taken,
        [int]$MaxAttempts = 50
    )
    $port = $StartPort
    for ($i = 0; $i -lt $MaxAttempts; $i++) {
        if (-not $Taken.Contains($port) -and (Test-PortFree -Port $port)) {
            return $port
        }
        $port++
    }
    throw "No free port found starting at $StartPort after $MaxAttempts attempts"
}

$apiBase = 8000
if ($env:API_HOST_PORT) { $apiBase = [int]$env:API_HOST_PORT }

$frontendBase = 3000
if ($env:FRONTEND_HOST_PORT) { $frontendBase = [int]$env:FRONTEND_HOST_PORT }

$taken = [System.Collections.Generic.HashSet[int]]::new()
$apiPort = Find-FreePort -StartPort $apiBase -Taken $taken
[void]$taken.Add($apiPort)
$frontendPort = Find-FreePort -StartPort $frontendBase -Taken $taken

if ($apiPort -ne $apiBase) {
    Write-Host "[docker:up] Port $apiBase is busy - backend will use $apiPort instead."
}
if ($frontendPort -ne $frontendBase) {
    Write-Host "[docker:up] Port $frontendBase is busy - frontend will use $frontendPort instead."
}
Write-Host "[docker:up] Backend:  http://localhost:$apiPort"
Write-Host "[docker:up] Frontend: http://localhost:$frontendPort"

$env:API_HOST_PORT = "$apiPort"
$env:FRONTEND_HOST_PORT = "$frontendPort"

docker compose up @args
exit $LASTEXITCODE
