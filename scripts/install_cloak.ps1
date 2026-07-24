# install_cloak.ps1 — Windows bring-up for Hermes Cloak Edition
#
# 1. Writes %USERPROFILE%\.hermes\cloak\manager.env (token + URLs)
# 2. Optionally starts CloakBrowser-Manager via Docker Desktop (:8080)
# 3. Starts the Python CDP auth bridge on :8081 (NOT nginx)
# 4. Installs cloakbrowser + playwright + httpx into the active Python
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_cloak.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_cloak.ps1 -NoManager
#   powershell -ExecutionPolicy Bypass -File scripts\install_cloak.ps1 -RegenerateToken

[CmdletBinding()]
param(
    [switch]$NoManager,
    [switch]$NoBridge,
    [switch]$RegenerateToken,
    [switch]$ConfigureProvider,
    [switch]$Strict,
    [string]$DockerImage = $(if ($env:CLOAK_DOCKER_IMAGE) { $env:CLOAK_DOCKER_IMAGE } else { "cloakhq/cloakbrowser-manager:latest" }),
    [string]$DockerName = $(if ($env:CLOAK_DOCKER_NAME) { $env:CLOAK_DOCKER_NAME } else { "cloakbrowser-manager" }),
    [int]$ManagerPort = $(if ($env:CLOAK_MANAGER_PORT) { [int]$env:CLOAK_MANAGER_PORT } else { 8080 }),
    [string]$ManagerUrl = "",
    [int]$BridgePort = $(if ($env:CLOAK_CDP_BRIDGE_PORT) { [int]$env:CLOAK_CDP_BRIDGE_PORT } else { 8081 }),
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" })
)

$ErrorActionPreference = "Stop"
$env:HERMES_HOME = $HermesHome
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Split-Path -Parent $ScriptDir
$CloakDir = Join-Path $env:USERPROFILE ".hermes\cloak"
$EnvFile = if ($env:CLOAK_MANAGER_ENV) { $env:CLOAK_MANAGER_ENV } else { Join-Path $CloakDir "manager.env" }
$BridgeScript = Join-Path $ScriptDir "cloak\cdp_bridge.py"
$HttpProbe = Join-Path $ScriptDir "cloak\http_probe.py"
$BridgeReadiness = Join-Path $ScriptDir "cloak\bridge_readiness.py"
$BridgePidFile = Join-Path $CloakDir "cdp_bridge.pid"
$managerReady = $false
$bridgeReady = $false
$cloakProvisionFailed = $false

function Write-Info([string]$msg) { Write-Host "[cloak] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg) { Write-Host "[cloak] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[cloak] $msg" -ForegroundColor Yellow }

function Get-ManagerEnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return "" }
    $pattern = "^\s*$([regex]::Escape($Name))=(.*)$"
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match $pattern) {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Set-ManagerEnvValue([string]$Name, [string]$Value) {
    $pattern = "^\s*$([regex]::Escape($Name))="
    $content = @()
    if (Test-Path -LiteralPath $EnvFile) {
        $content = @(Get-Content -LiteralPath $EnvFile)
    }
    $updated = $false
    $next = foreach ($line in $content) {
        if ($line -match $pattern) {
            $updated = $true
            "$Name=$Value"
        } else {
            $line
        }
    }
    if (-not $updated) { $next += "$Name=$Value" }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($EnvFile, @($next), $utf8NoBom)
}

function Protect-ManagerEnvFile {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $false }
    $icacls = Get-Command icacls.exe -ErrorAction SilentlyContinue
    if (-not $icacls) { $icacls = Get-Command icacls -ErrorAction SilentlyContinue }
    if (-not $icacls) {
        Write-Warn "icacls is unavailable; could not restrict access to $EnvFile"
        return $false
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($identity)) {
        Write-Warn "Could not resolve the current Windows identity for $EnvFile"
        return $false
    }
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $grant = "{0}:(F)" -f $identity
        & $icacls.Source $EnvFile "/inheritance:r" "/grant:r" $grant "/grant:r" "SYSTEM:(F)" *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previous
    }
}
function Clear-CdpProxyBase {
    if (Test-Path -LiteralPath $EnvFile) {
        $content = @(Get-Content -LiteralPath $EnvFile | Where-Object { $_ -notmatch '^CLOAK_CDP_PROXY_BASE=' })
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllLines($EnvFile, $content, $utf8NoBom)
    }
    [Environment]::SetEnvironmentVariable("CLOAK_CDP_PROXY_BASE", $null, "Process")
    Remove-Item -Path "Env:CLOAK_CDP_PROXY_BASE" -ErrorAction SilentlyContinue
}

function Publish-CdpProxyBase([string]$BaseUrl) {
    Set-ManagerEnvValue "CLOAK_CDP_PROXY_BASE" $BaseUrl
    [Environment]::SetEnvironmentVariable("CLOAK_CDP_PROXY_BASE", $BaseUrl, "Process")
    Set-Item -Path "Env:CLOAK_CDP_PROXY_BASE" -Value $BaseUrl
}

function Ensure-ManagerAllowedHost([Uri]$ManagerUri) {
    $managerHost = ([string]$ManagerUri.Host).Trim().ToLowerInvariant()
    if (-not $managerHost -or $managerHost -in @("localhost", "127.0.0.1", "::1")) { return }

    $entries = @((Get-ManagerEnvValue "CLOAK_ALLOWED_HOSTS") -split ',' |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ })
    $exists = $false
    foreach ($entry in $entries) {
        if ($entry.Equals($managerHost, [System.StringComparison]::OrdinalIgnoreCase)) {
            $exists = $true
            break
        }
    }
    if (-not $exists) { $entries += $managerHost }

    $allowedHosts = $entries -join ','
    Set-ManagerEnvValue "CLOAK_ALLOWED_HOSTS" $allowedHosts
    [Environment]::SetEnvironmentVariable("CLOAK_ALLOWED_HOSTS", $allowedHosts, "Process")
    Set-Item -Path "Env:CLOAK_ALLOWED_HOSTS" -Value $allowedHosts
}
function Normalize-ManagerUrl([string]$Value) {
    $candidate = ([string]$Value).Trim().TrimEnd('/')
    if (-not $candidate) { throw "CLOAK_MANAGER_URL is empty" }
    if ($candidate -notmatch '^[a-zA-Z][a-zA-Z0-9+.-]*://') {
        $candidate = "http://$candidate"
    }
    try { $uri = [Uri]$candidate } catch { throw "Invalid CLOAK_MANAGER_URL" }
    if (($uri.Scheme -ne "http" -and $uri.Scheme -ne "https") -or -not $uri.Host) {
        throw "CLOAK_MANAGER_URL must be an http(s) URL"
    }
    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "CLOAK_MANAGER_URL must not contain credentials, a query, or a fragment"
    }
    return $candidate
}

function Test-ThisInstallCdpBridge([int]$CandidatePid) {
    if ($CandidatePid -le 0 -or -not (Test-Path -LiteralPath $BridgeScript)) { return $false }
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $CandidatePid" -ErrorAction Stop
        $command = [string]$process.CommandLine
        $scriptPath = [string](Resolve-Path -LiteralPath $BridgeScript -ErrorAction Stop).Path
        return $command.Contains($scriptPath) -and $command.Contains("http://127.0.0.1:$BridgePort")
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @(
        (Join-Path $InstallDir "venv\Scripts\python.exe"),
        (Join-Path $InstallDir ".venv\Scripts\python.exe")
    )
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    return $null
}
function Get-NpmCommand {
    foreach ($candidate in @(
        (Join-Path $HermesHome "node\npm.cmd"),
        (Join-Path $HermesHome "node\bin\npm.cmd")
    )) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    foreach ($name in @("npm.cmd", "npm.exe", "npm")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

function Test-AgentBrowser([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate)) { return $false }
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Candidate --help *> $null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Find-AgentBrowser {
    $candidates = @(
        (Join-Path $InstallDir "node_modules\.bin\agent-browser.cmd"),
        (Join-Path $InstallDir "node_modules\.bin\agent-browser"),
        (Join-Path $HermesHome "node\agent-browser.cmd"),
        (Join-Path $HermesHome "node\agent-browser")
    )
    foreach ($candidate in $candidates) {
        if (Test-AgentBrowser $candidate) { return $candidate }
    }
    return $null
}

function Get-PythonInstaller([string]$Python) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -m pip --version *> $null
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ Kind = "pip"; Command = $null } }

        & $Python -m ensurepip --upgrade *> $null
        & $Python -m pip --version *> $null
        if ($LASTEXITCODE -eq 0) { return [pscustomobject]@{ Kind = "pip"; Command = $null } }
    } finally {
        $ErrorActionPreference = $previous
    }

    $managedUv = Join-Path $HermesHome "bin\uv.exe"
    if (Test-Path -LiteralPath $managedUv) {
        return [pscustomobject]@{ Kind = "uv"; Command = $managedUv }
    }

    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $uv) { $uv = Get-Command uv -ErrorAction SilentlyContinue }
    if ($uv) { return [pscustomobject]@{ Kind = "uv"; Command = $uv.Source } }
    return $null
}

function Install-CloakPythonPackages([string]$Python) {
    Write-Info "Installing cloakbrowser / playwright / httpx..."
    $installer = Get-PythonInstaller $Python
    if (-not $installer) {
        Write-Warn "Neither pip nor uv is available for the Hermes Python environment."
        return $false
    }

    $exitCode = 1
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        if ($installer.Kind -eq "pip") {
            & $Python -m pip install --upgrade --quiet "cloakbrowser>=0.3" "playwright>=1.53" "httpx>=0.27"
        } else {
            & $installer.Command pip install --python $Python --upgrade --quiet "cloakbrowser>=0.3" "playwright>=1.53" "httpx>=0.27"
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -eq 0) {
        Write-Ok "Python Cloak deps installed; local Playwright Chromium is intentionally skipped."
        return $true
    }
    Write-Warn "Cloak Python dependency installation failed."
    return $false
}

function Ensure-AgentBrowser {
    if (Find-AgentBrowser) {
        Write-Ok "agent-browser is available"
        return $true
    }
    $npm = Get-NpmCommand
    if (-not $npm -or -not (Test-Path -LiteralPath (Join-Path $InstallDir "package.json"))) {
        Write-Warn "npm or the Hermes package manifest is missing; agent-browser is unavailable."
        return $false
    }

    Write-Info "Installing Node browser dependencies (agent-browser)..."
    $exitCode = 1
    $previous = $ErrorActionPreference
    $pushedLocation = $false
    try {
        Push-Location $InstallDir
        $pushedLocation = $true
        $ErrorActionPreference = "Continue"
        & $npm install --silent --no-fund --no-audit
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        if ($pushedLocation) { Pop-Location }
    }
    if ($exitCode -ne 0 -or -not (Find-AgentBrowser)) {
        Write-Warn "agent-browser could not be installed."
        return $false
    }
    Write-Ok "agent-browser installed"
    return $true
}

function Set-CloakProvider {
    $candidates = @(
        (Join-Path $InstallDir "venv\Scripts\hermes.exe"),
        (Join-Path $InstallDir ".venv\Scripts\hermes.exe")
    )
    $hermes = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $hermes) {
        Write-Warn "Hermes CLI was not found in the installed virtual environment."
        return $false
    }
    $previousHome = $env:HERMES_HOME
    try {
        $env:HERMES_HOME = $HermesHome
        & $hermes config set browser.cloud_provider cloak
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Could not set browser.cloud_provider=cloak."
            return $false
        }
    } finally {
        $env:HERMES_HOME = $previousHome
    }
    Write-Ok "Configured browser.cloud_provider=cloak"
    return $true
}

function Stop-CdpBridge {
    if (-not (Test-Path -LiteralPath $BridgePidFile)) { return }
    $oldPid = 0
    try {
        $raw = (Get-Content -LiteralPath $BridgePidFile -Raw -ErrorAction Stop).Trim()
        if ($raw) {
            try {
                $metadata = $raw | ConvertFrom-Json -ErrorAction Stop
                $oldPid = [int]$metadata.pid
            } catch {
                $oldPid = [int]$raw
            }
        }
    } catch { $oldPid = 0 }
    if ($oldPid -gt 0 -and (Test-ThisInstallCdpBridge $oldPid)) {
        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
        Write-Info "Stopped this install's previous CDP bridge pid=$oldPid"
        Start-Sleep -Milliseconds 400
    } elseif ($oldPid -gt 0) {
        Write-Warn "PID file does not identify this install's bridge; leaving pid=$oldPid untouched"
    }
    Remove-Item -LiteralPath $BridgePidFile -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force -Path $CloakDir | Out-Null

$initialManagerUrl = if ($PSBoundParameters.ContainsKey("ManagerUrl")) {
    Normalize-ManagerUrl $ManagerUrl
} else {
    Normalize-ManagerUrl "http://127.0.0.1:$ManagerPort"
}
$needToken = $RegenerateToken -or -not (Test-Path -LiteralPath $EnvFile)
if ($needToken) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $lines = @(
        "CLOAK_MANAGER_URL=$initialManagerUrl"
        "CLOAK_AUTH_TOKEN=$token"
        "CAPSOLVER_API_KEY="
        "TWOCAPTCHA_API_KEY="
        "TWO_CAPTCHA_API_KEY="
        "NOTLETTERS_API_KEY="
        "CLOAK_USE_PROXY_POOL=0"
        "CLOAK_IDLE_TIMEOUT_MIN=0"
    )
    # UTF-8 without BOM — PowerShell Set-Content -Encoding UTF8 writes BOM on Windows PowerShell 5.x
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($EnvFile, $lines, $utf8NoBom)
    Write-Ok "Created $EnvFile"
} else {
    Write-Ok "Keeping existing $EnvFile"
}

$managerEnvProtected = Protect-ManagerEnvFile
if (-not $managerEnvProtected) {
    Write-Warn "Manager env file ACL could not be verified."
}
if ($Strict -and -not $managerEnvProtected) {
    throw "Could not protect the Manager env file in strict mode."
}
# File is source of truth for the installer — always overwrite Process env for Cloak keys.
# (A stale CLOAK_AUTH_TOKEN in Process env previously beat a freshly regenerated file.)
Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim().TrimStart([char]0xFEFF)
    if (-not $line -or $line.StartsWith("#") -or ($line -notmatch "=")) { return }
    $parts = $line.Split("=", 2)
    $k = $parts[0].Trim().TrimStart([char]0xFEFF)
    $v = $parts[1].Trim().Trim('"').Trim("'")
    if ($k) {
        [Environment]::SetEnvironmentVariable($k, $v, "Process")
        Set-Item -Path "Env:$k" -Value $v
    }
}
$token = [Environment]::GetEnvironmentVariable("CLOAK_AUTH_TOKEN", "Process")
if (-not $token) { throw "CLOAK_AUTH_TOKEN missing in $EnvFile" }

$storedManagerUrl = Get-ManagerEnvValue "CLOAK_MANAGER_URL"
if ($PSBoundParameters.ContainsKey("ManagerUrl")) {
    $managerUrl = Normalize-ManagerUrl $ManagerUrl
} elseif ($PSBoundParameters.ContainsKey("ManagerPort")) {
    $managerUrl = Normalize-ManagerUrl "http://127.0.0.1:$ManagerPort"
} elseif ($storedManagerUrl) {
    $managerUrl = Normalize-ManagerUrl $storedManagerUrl
} else {
    $managerUrl = $initialManagerUrl
}
Set-ManagerEnvValue "CLOAK_MANAGER_URL" $managerUrl
[Environment]::SetEnvironmentVariable("CLOAK_MANAGER_URL", $managerUrl, "Process")
Set-Item -Path "Env:CLOAK_MANAGER_URL" -Value $managerUrl
$managerUri = [Uri]$managerUrl
$managerHost = $managerUri.Host.ToLowerInvariant()
$isLocalManager = $managerHost -eq "localhost" -or $managerHost -eq "127.0.0.1" -or $managerHost -eq "::1"
Ensure-ManagerAllowedHost $managerUri
$managesDocker = $isLocalManager -and $managerUri.Scheme -eq "http"
if ($managesDocker) {
    $ManagerPort = $managerUri.Port
}

if (-not $NoManager -and $managesDocker) {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        if ($RegenerateToken) {
            $existsForRecreate = docker ps -a --format "{{.Names}}" 2>$null | Select-String -SimpleMatch -Pattern $DockerName
            if ($existsForRecreate) {
                Write-Info "Token regenerated - recreating container $DockerName with new AUTH_TOKEN..."
                docker rm -f $DockerName 2>$null | Out-Null
            }
        }
        $running = docker ps --format "{{.Names}}" 2>$null | Select-String -SimpleMatch -Pattern $DockerName
        if ($running) {
            Write-Ok "Container $DockerName already running"
        } else {
            $exists = docker ps -a --format "{{.Names}}" 2>$null | Select-String -SimpleMatch -Pattern $DockerName
            if ($exists) {
                docker start $DockerName | Out-Null
                Write-Ok "Started existing container $DockerName"
            } else {
                Write-Info "Pulling $DockerImage ..."
                docker pull $DockerImage
                Write-Info "Starting $DockerName on 127.0.0.1:$ManagerPort ..."
                docker run -d --name $DockerName --restart unless-stopped `
                    -p "127.0.0.1:${ManagerPort}:8080" `
                    -v "cloak-profiles:/data" `
                    -e "AUTH_TOKEN=$token" `
                    $DockerImage | Out-Null
                Write-Ok "Container started"
            }
        }
        $ready = $false
        for ($i = 0; $i -lt 15; $i++) {
            try {
                $r = Invoke-WebRequest -Uri "$managerUrl/api/profiles" `
                    -Headers @{ Authorization = "Bearer $token" } -UseBasicParsing -TimeoutSec 3
                if ($r.StatusCode -eq 200) { $ready = $true; break }
            } catch {
                Start-Sleep -Seconds 1
            }
        }
        if ($ready) {
            $managerReady = $true
            Write-Ok "CloakBrowser-Manager up on $managerUrl"
        } else {
            Clear-CdpProxyBase
            $cloakProvisionFailed = $true
            Write-Warn "Manager did not pass protected readiness - CLOAK_CDP_PROXY_BASE cleared"
        }
    } else {
        Write-Warn "Docker not found. Install Docker Desktop or point CLOAK_MANAGER_URL at a remote Manager."
    }
} elseif (-not $NoManager) {
    Write-Info "Using external CloakBrowser-Manager at $managerUrl"
    try {
        $r = Invoke-WebRequest -Uri "$managerUrl/api/profiles" `
            -Headers @{ Authorization = "Bearer $token" } -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) {
            $managerReady = $true
            Write-Ok "External CloakBrowser-Manager is ready"
        } else {
            Clear-CdpProxyBase
            $cloakProvisionFailed = $true
            Write-Warn "External Manager returned HTTP $($r.StatusCode) on protected readiness probe - CLOAK_CDP_PROXY_BASE cleared"
        }
    } catch {
        Clear-CdpProxyBase
        $cloakProvisionFailed = $true
        Write-Warn "External Manager did not pass protected readiness probe: $($_.Exception.Message)"
    }
} else {
    Write-Info "Skipping manager (-NoManager)"
}

if (-not $NoManager -and -not $managerReady) {
    Clear-CdpProxyBase
    $cloakProvisionFailed = $true
}

if ($NoBridge) {
    Stop-CdpBridge
    Clear-CdpProxyBase
    $bridgeReady = $true # Explicit opt-out, not a failed readiness check.
    Write-Info "Skipping CDP bridge (-NoBridge); CLOAK_CDP_PROXY_BASE cleared"
} elseif (-not (Test-Path -LiteralPath $BridgeScript) -or -not (Test-Path -LiteralPath $BridgeReadiness)) {
    Stop-CdpBridge
    Clear-CdpProxyBase
    $cloakProvisionFailed = $true
    Write-Warn "CDP bridge readiness scripts missing - CLOAK_CDP_PROXY_BASE cleared"
} else {
        Stop-CdpBridge
        Write-Info "Starting CDP bridge on 127.0.0.1:$BridgePort (background)..."
        $py = Find-Python
        if (-not $py) { throw "Python not found" }
        # Token via env only — do not put Bearer on the process command line.
        $env:CLOAK_AUTH_TOKEN = $token
        $env:CLOAK_MANAGER_URL = $managerUrl
        $proc = $null
        try {
            $proc = Start-Process -FilePath $py -ArgumentList @(
            $BridgeScript,
            "--listen", "http://127.0.0.1:$BridgePort",
            "--upstream", $managerUrl
            ) -WindowStyle Hidden -PassThru -ErrorAction Stop
        } catch {
            Write-Warn "Could not start CDP bridge: $($_.Exception.Message)"
        }
        if ($proc -and $proc.Id) {
            $metadata = [ordered]@{
                pid = $proc.Id
                bridge_script = [string](Resolve-Path -LiteralPath $BridgeScript).Path
                listen = "http://127.0.0.1:$BridgePort"
                upstream = $managerUrl
            } | ConvertTo-Json -Compress
            $utf8NoBom = New-Object System.Text.UTF8Encoding $false
            [System.IO.File]::WriteAllText($BridgePidFile, $metadata, $utf8NoBom)
        }
        $bridgeOk = $false
        if ($proc) {
            Start-Sleep -Seconds 1

        # Auth-proxy readiness = protected HTTP /api/profiles through the bridge returns 200.
        # Do NOT probe ws://host:8081/ — Manager CDP WS lives only under
        # /api/profiles/{id}/cdp and is unavailable without a running profile.
        $statusUrl = "http://127.0.0.1:$BridgePort/api/profiles"
        if (Test-Path -LiteralPath $HttpProbe) {
            & $py $HttpProbe --url $statusUrl --timeout 5
            if ($LASTEXITCODE -eq 0) { $bridgeOk = $true }
        }
        if (-not $bridgeOk) {
            try {
                $r = Invoke-WebRequest -Uri $statusUrl -UseBasicParsing -TimeoutSec 5
                if ($r.StatusCode -eq 200) { $bridgeOk = $true }
            } catch {
                $bridgeOk = $false
        }
        }
        }

        if ($bridgeOk) {
            & $py $BridgeReadiness --manager-url $managerUrl --bridge-url "http://127.0.0.1:$BridgePort" --timeout 5
            if ($LASTEXITCODE -ne 0) { $bridgeOk = $false }
        }

        if ($bridgeOk) {
            $bridgeReady = $true
            Publish-CdpProxyBase "http://127.0.0.1:$BridgePort"
            Write-Ok "CDP bridge ready - CLOAK_CDP_PROXY_BASE published"
        } else {
            Clear-CdpProxyBase
            $cloakProvisionFailed = $true
            Write-Warn "CDP bridge not ready - CLOAK_CDP_PROXY_BASE cleared"
        }
    }

$py = Find-Python
$pythonDepsReady = $false
if ($py) {
    $pythonDepsReady = Install-CloakPythonPackages $py
} else {
    Write-Warn "Python not found for Cloak dependencies"
}

$nodeBrowserReady = Ensure-AgentBrowser
if ($Strict -and (-not $pythonDepsReady -or -not $nodeBrowserReady)) {
    Stop-CdpBridge
    Clear-CdpProxyBase
    $cloakProvisionFailed = $true
    Write-Warn "Strict Cloak dependencies are incomplete; no ready state will be reported."
}

if ($cloakProvisionFailed) {
    throw "Cloak did not pass protected Manager/CDP bridge readiness or strict provisioning; no ready release state was published."
}

if ($ConfigureProvider -and -not (Set-CloakProvider)) {
    throw "Cloak provider configuration failed."
}

if ($ConfigureProvider) {
    Write-Ok "Done. browser.cloud_provider=cloak is configured."
} else {
    Write-Ok "Done. Run 'hermes config set browser.cloud_provider cloak' to enable the provider."
}
Write-Info "Env file: $EnvFile"
Write-Info "Skill: skills/cloak-proxy-pool"
Write-Info "Token is stored in the env file only - it is not printed here."
