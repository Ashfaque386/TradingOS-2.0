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
# Output capture uses Start-Process with -RedirectStandardOutput /
# -RedirectStandardError (.NET Process class redirection), not any of
# PowerShell's own native-command redirection operators (`2>&1 | ...`,
# `*> file`). Every one of those was tried, live, against this exact
# operator's environment, and every one of them let PowerShell intercept
# and garble/drop native stderr text (rendered as "docker.exe : ...
# NativeCommandError" noise, with the real "Bind for ... failed" error
# sometimes never reaching the captured text at all) regardless of
# $ErrorActionPreference. Start-Process's redirection happens entirely in
# .NET, outside PowerShell's native-command pipeline, so it is not subject
# to any of that — this is not a preference to tune, it's a different
# mechanism. Do not go back to `&`/pipe-based capture for this script.
#
# Usage (from the repo root):
#   .\scripts\docker-up.ps1 -d --build
# or, if PowerShell's execution policy blocks running scripts:
#   .\scripts\docker-up.cmd -d --build

$BackendContainer = 'TradingOS-2.0-Backend'
$BackendContainerPort = 8000
$FrontendContainer = 'TradingOS-2.0-Frontend'
$FrontendContainerPort = 3000

function Invoke-DockerCommand {
    param([string[]]$DockerArgs)

    $stdoutFile = [System.IO.Path]::GetTempFileName()
    $stderrFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath 'docker' -ArgumentList $DockerArgs -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $stdoutText = Get-Content -Path $stdoutFile -Raw -ErrorAction SilentlyContinue
        $stderrText = Get-Content -Path $stderrFile -Raw -ErrorAction SilentlyContinue
        if (-not $stdoutText) { $stdoutText = '' }
        if (-not $stderrText) { $stderrText = '' }
        return @{ Code = $proc.ExitCode; StdOut = $stdoutText; StdErr = $stderrText; Output = "$stdoutText`n$stderrText" }
    } finally {
        Remove-Item -Path $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-DockerComposeUp {
    param([int]$ApiPort, [int]$FrontendPort, [string[]]$ExtraArgs)

    $env:API_HOST_PORT = "$ApiPort"
    $env:FRONTEND_HOST_PORT = "$FrontendPort"

    $result = Invoke-DockerCommand -DockerArgs (@('compose', 'up') + $ExtraArgs)
    Write-Host $result.Output
    return $result
}

function Test-PortPublished {
    param([string]$ContainerName, [int]$ContainerPort)
    # stdout only: a published port always prints its mapping to stdout,
    # while an unpublished one can write an error message to stderr (Docker
    # version-dependent) -- checking combined output would wrongly read
    # that stderr text as "published".
    $result = Invoke-DockerCommand -DockerArgs @('port', $ContainerName, "$ContainerPort")
    return [bool]($result.StdOut.Trim())
}

function Remove-ComposeContainer {
    param([string]$Service)
    Invoke-DockerCommand -DockerArgs @('compose', 'rm', '-f', $Service) | Out-Null
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

    # Only the specific line naming the failing endpoint may be checked for
    # which service it is -- `docker compose up`'s surrounding output lists
    # *every* service's container transitioning through
    # Creating/Starting/Started (including "TradingOS-2.0-Frontend") even
    # on a completely unrelated conflict (e.g. Prometheus/Grafana), so
    # matching against the whole captured $result.Output previously
    # misattributed any non-backend/non-frontend port conflict to the
    # frontend and only ever bumped FrontendPort, silently never fixing
    # the real conflict.
    $conflictLine = ($result.Output -split "`n") | Where-Object { $_ -match 'port is already allocated' } | Select-Object -First 1

    if ($conflictLine -and $conflictLine -match 'Bind for [\d\.]+:(\d+) failed: port is already allocated') {
        $conflictPort = [int]$Matches[1]
        $isFrontend = $conflictLine -match 'TradingOS-2\.0-Frontend'
        $isBackend = $conflictLine -match 'TradingOS-2\.0-Backend'

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
        Write-Host "[docker:up] docker compose up failed for a reason other than a port conflict (exit $($result.Code)) - not retrying."
        Write-Host "[docker:up] Captured output was $($result.Output.Length) chars; last 1000 shown below for diagnosis:"
        Write-Host $result.Output.Substring([Math]::Max(0, $result.Output.Length - 1000))
        exit $result.Code
    }
}

Write-Host "[docker:up] Still failing after $maxAttempts attempts."
exit 1
