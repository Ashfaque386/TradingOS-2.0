# Windows-native equivalent of scripts/docker-up.js — for machines without
# Node/pnpm installed. Wraps `docker compose up`: if any of this stack's
# four published host ports (backend/frontend/prometheus/grafana) is
# already taken by something else on the machine, bumps that one port and
# retries automatically. Pure PowerShell, no extra tooling.
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

# One entry per service this stack publishes a host port for. EnvVar is
# what docker-compose.yml's `${...}` substitution reads; ComposeService is
# the service name `docker compose rm -f <name>` takes. ContainerPort is
# the port to check with `docker port` (grafana's own image always
# listens on 3000 internally, even though its default host port is 3001).
$Services = @(
    @{ Key = 'backend'; ContainerName = 'TradingOS-2.0-Backend'; ContainerPort = 8000; EnvVar = 'API_HOST_PORT'; ComposeService = 'backend'; DefaultPort = 8000 },
    @{ Key = 'frontend'; ContainerName = 'TradingOS-2.0-Frontend'; ContainerPort = 3000; EnvVar = 'FRONTEND_HOST_PORT'; ComposeService = 'frontend'; DefaultPort = 3000 },
    @{ Key = 'prometheus'; ContainerName = 'TradingOS-2.0-Prometheus'; ContainerPort = 9090; EnvVar = 'PROMETHEUS_HOST_PORT'; ComposeService = 'prometheus'; DefaultPort = 9090 },
    @{ Key = 'grafana'; ContainerName = 'TradingOS-2.0-Grafana'; ContainerPort = 3000; EnvVar = 'GRAFANA_HOST_PORT'; ComposeService = 'grafana'; DefaultPort = 3001 }
)

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
    param([hashtable]$Ports, [string[]]$ExtraArgs)

    foreach ($service in $Services) {
        [Environment]::SetEnvironmentVariable($service.EnvVar, "$($Ports[$service.Key])")
    }

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

$ports = @{}
foreach ($service in $Services) {
    $envValue = [Environment]::GetEnvironmentVariable($service.EnvVar)
    if ($envValue) { $ports[$service.Key] = [int]$envValue } else { $ports[$service.Key] = $service.DefaultPort }
}

$extraArgs = $args
$maxAttempts = 20

for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    $portSummary = ($Services | ForEach-Object { "$($_.Key)=$($ports[$_.Key])" }) -join ' '
    Write-Host "[docker:up] Attempt ${attempt}: $portSummary"
    $result = Invoke-DockerComposeUp -Ports $ports -ExtraArgs $extraArgs

    if ($result.Code -eq 0) {
        $notPublished = @()
        foreach ($service in $Services) {
            $ok = Test-PortPublished -ContainerName $service.ContainerName -ContainerPort $service.ContainerPort
            if (-not $ok) { $notPublished += $service }
        }

        if ($notPublished.Count -eq 0) {
            foreach ($service in $Services) {
                Write-Host "[docker:up] $($service.Key): http://localhost:$($ports[$service.Key])"
            }
            exit 0
        }

        $service = $notPublished[0]
        $ports[$service.Key]++
        Write-Host "[docker:up] $($service.Key) port didn't actually publish - bumping to $($ports[$service.Key]) and retrying..."
        Remove-ComposeContainer -Service $service.ComposeService
        continue
    }

    # Only the specific line naming the failing endpoint may be checked for
    # which service it is -- `docker compose up`'s surrounding output lists
    # *every* service's container transitioning through
    # Creating/Starting/Started (including every container name this
    # stack has) regardless of which one's port actually conflicted, so
    # matching against the whole captured $result.Output previously
    # misattributed any conflict to whichever service happened to be
    # mentioned first (nearly always the frontend) and only ever bumped
    # that one port, silently never fixing the real conflict.
    $conflictLine = ($result.Output -split "`n") | Where-Object { $_ -match 'port is already allocated' } | Select-Object -First 1

    if ($conflictLine -and $conflictLine -match 'Bind for [\d\.]+:(\d+) failed: port is already allocated') {
        $conflictPort = [int]$Matches[1]
        $matchedService = $Services | Where-Object { $conflictLine -match [regex]::Escape($_.ContainerName) } | Select-Object -First 1
        if (-not $matchedService) {
            $matchedService = $Services | Where-Object { $ports[$_.Key] -eq $conflictPort } | Select-Object -First 1
        }

        if ($matchedService) {
            $ports[$matchedService.Key]++
            Write-Host "[docker:up] Port busy - bumping $($matchedService.Key) to $($ports[$matchedService.Key]) and retrying..."
            Remove-ComposeContainer -Service $matchedService.ComposeService
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
