# Windows-native equivalent of scripts/docker-up.js — for machines without
# Node/pnpm installed. Wraps `docker compose up`: if API_HOST_PORT (default
# 8000) or FRONTEND_HOST_PORT (default 3000) is already taken, bumps to the
# next port and retries automatically. Pure PowerShell, no extra tooling.
#
# This retries against docker compose's *actual* failure rather than
# pre-checking port availability with a TCP bind test: on Docker Desktop /
# WSL2, a port can be reserved in Docker's own network layer without
# showing up as "in use" to a normal Windows socket bind, so a pre-check
# can wrongly report a busy port as free (confirmed live). Retrying on the
# real error from `docker compose up` is the only reliable source of truth.
#
# It also does not trust a 0 exit code alone: `docker compose up -d` has
# been observed to exit 0 while a container's port silently failed to
# publish (confirmed live), so every "success" is double-checked with
# `docker port <container> <port>` before being declared real.
#
# Output is captured via file redirection (`*>`), not a pipeline (`2>&1 |
# ...`): piping a native command's stderr through the PowerShell pipeline
# wraps each line in a NativeCommandError object (visible as garbled
# "docker.exe : ... NativeCommandError" noise, confirmed live), which can
# corrupt the exact text this script matches against. File redirection
# captures the raw text untouched.
#
# Even with file redirection, an $ErrorActionPreference of 'Stop' (or, on
# PowerShell 7.3+, $PSNativeCommandUseErrorActionPreference) makes
# PowerShell intercept a native command's stderr writes as terminating
# errors and display them via its own NativeCommandError formatting
# *instead of* letting them flow into the redirect target — confirmed
# live: Docker BuildKit's normal (non-error) progress output goes to
# stderr, and with a strict $ErrorActionPreference the real "Bind for ...
# failed" error text never made it into the captured file at all, so it
# was never detected. Both preferences are reset below so this script's
# behavior does not depend on whatever the caller's profile/session set.
$ErrorActionPreference = 'Continue'
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
#
# Usage (from the repo root):
#   .\scripts\docker-up.ps1 -d --build
# or, if PowerShell's execution policy blocks running scripts:
#   .\scripts\docker-up.cmd -d --build

$BackendContainer = 'TradingOS-2.0-Backend'
$BackendContainerPort = 8000
$FrontendContainer = 'TradingOS-2.0-Frontend'
$FrontendContainerPort = 3000

function Invoke-DockerComposeUp {
    param([int]$ApiPort, [int]$FrontendPort, [string[]]$ExtraArgs)

    $env:API_HOST_PORT = "$ApiPort"
    $env:FRONTEND_HOST_PORT = "$FrontendPort"

    $allArgs = @('compose', 'up') + $ExtraArgs
    $tempFile = [System.IO.Path]::GetTempFileName()
    try {
        & docker @allArgs *> $tempFile
        $code = $LASTEXITCODE
        $output = Get-Content -Path $tempFile -Raw -ErrorAction SilentlyContinue
        if (-not $output) { $output = '' }
    } finally {
        Remove-Item -Path $tempFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host $output
    return @{ Code = $code; Output = $output }
}

function Test-PortPublished {
    param([string]$ContainerName, [int]$ContainerPort)
    $portOutput = & docker port $ContainerName $ContainerPort 2>$null
    return [bool]$portOutput
}

function Remove-ComposeContainer {
    param([string]$Service)
    & docker compose rm -f $Service *> $null
}

$apiPort = 8000
if ($env:API_HOST_PORT) { $apiPort = [int]$env:API_HOST_PORT }

$frontendPort = 3000
if ($env:FRONTEND_HOST_PORT) { $frontendPort = [int]$env:FRONTEND_HOST_PORT }

$extraArgs = $args
$maxAttempts = 20

for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    Write-Host "[docker:up] Attempt ${attempt}: backend=$apiPort frontend=$frontendPort"
    $result = Invoke-DockerComposeUp -ApiPort $apiPort -FrontendPort $frontendPort -ExtraArgs $extraArgs

    if ($result.Code -eq 0) {
        $backendOk = Test-PortPublished -ContainerName $BackendContainer -ContainerPort $BackendContainerPort
        $frontendOk = Test-PortPublished -ContainerName $FrontendContainer -ContainerPort $FrontendContainerPort

        if ($backendOk -and $frontendOk) {
            Write-Host "[docker:up] Backend:  http://localhost:$apiPort"
            Write-Host "[docker:up] Frontend: http://localhost:$frontendPort"
            exit 0
        }

        if (-not $frontendOk) {
            $frontendPort++
            Write-Host "[docker:up] Frontend port didn't actually publish - bumping to $frontendPort and retrying..."
            Remove-ComposeContainer -Service 'frontend'
        } else {
            $apiPort++
            Write-Host "[docker:up] Backend port didn't actually publish - bumping to $apiPort and retrying..."
            Remove-ComposeContainer -Service 'backend'
        }
        continue
    }

    if ($result.Output -match 'Bind for [\d\.]+:(\d+) failed: port is already allocated') {
        $conflictPort = [int]$Matches[1]
        $isFrontend = $result.Output -match 'TradingOS-2\.0-Frontend'
        $isBackend = $result.Output -match 'TradingOS-2\.0-Backend'

        if ($isFrontend -or $conflictPort -eq $frontendPort) {
            $frontendPort++
            Write-Host "[docker:up] Port busy - bumping frontend to $frontendPort and retrying..."
            Remove-ComposeContainer -Service 'frontend'
        } elseif ($isBackend -or $conflictPort -eq $apiPort) {
            $apiPort++
            Write-Host "[docker:up] Port busy - bumping backend to $apiPort and retrying..."
            Remove-ComposeContainer -Service 'backend'
        } else {
            Write-Host "[docker:up] Port conflict on $conflictPort but could not tell which service - aborting."
            exit $result.Code
        }
    } else {
        Write-Host "[docker:up] docker compose up failed for a reason other than a port conflict - not retrying."
        exit $result.Code
    }
}

Write-Host "[docker:up] Still failing after $maxAttempts attempts."
exit 1
