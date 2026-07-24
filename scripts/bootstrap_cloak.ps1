# One-command Windows deployment for Hermes Unlocked - Cloak Edition.
#
# Modes:
#   native  - Hermes runs on Windows; CloakBrowser Manager runs in Docker.
#   compose - Hermes (gateway + dashboard), Manager and the CDP bridge run as one
#              isolated Docker Compose project.

[CmdletBinding()]
param(
    [string]$Branch = "main",
    [ValidateSet("auto", "native", "compose")]
    [string]$Mode = "auto",
    [string]$HermesHome = "",
    [string]$InstallDir = "",
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')]
    [string]$ComposeProjectName = $(if ($env:CLOAK_COMPOSE_PROJECT) { $env:CLOAK_COMPOSE_PROJECT } else { "hermes-cloak-edition" }),
    [switch]$SkipDockerInstall,
    [ValidateRange(30, 600)]
    [int]$DockerTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$RawRepository = "https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19"
$ArchiveRepository = "https://github.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19"

function Write-Info([string]$Message) { Write-Host "[cloak-bootstrap] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[cloak-bootstrap] $Message" -ForegroundColor Green }
function Stop-Bootstrap([string]$Message) { throw "[cloak-bootstrap] $Message" }

function Test-SafeBranch([string]$Value) {
    return $Value -match '^[A-Za-z0-9._/-]+$' -and -not $Value.Contains('..') -and -not $Value.StartsWith('-')
}

function Resolve-InstallMode([string]$RequestedMode) {
    if ($RequestedMode -ne "auto") { return $RequestedMode }

    if (-not [Environment]::UserInteractive) {
        Write-Info "Non-interactive shell detected; using native mode. Pass -Mode compose to deploy the full Docker stack."
        return "native"
    }

    Write-Host ""
    Write-Host "Choose Cloak Edition installation mode:" -ForegroundColor White
    Write-Host "  1) native  - Hermes on Windows + CloakBrowser Manager in Docker (default)"
    Write-Host "  2) compose - Hermes, dashboard, CDP bridge and Manager in one Docker project"
    $selection = Read-Host "Enter 1 or 2"
    if ($selection -match '^(2|compose)$') { return "compose" }
    return "native"
}

function Sync-ProcessPath {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $pathParts = @($userPath, $machinePath, $env:Path) | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_)
    }
    $env:Path = $pathParts -join ';'
}

function Get-DockerCommand {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command docker -ErrorAction SilentlyContinue }
    if ($command) { return $command.Source }
    return $null
}

function Test-DockerReady {
    $docker = Get-DockerCommand
    if (-not $docker) { return $false }
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $docker version --format '{{.Server.Version}}' *> $null
        return $LASTEXITCODE -eq 0
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Ensure-DockerDesktop {
    if (Test-DockerReady) {
        Write-Ok "Docker Desktop is ready"
        return
    }

    $docker = Get-DockerCommand
    if (-not $docker -and -not $SkipDockerInstall) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { $winget = Get-Command winget -ErrorAction SilentlyContinue }
        if (-not $winget) {
            Stop-Bootstrap "Docker Desktop is missing and winget is unavailable. Install Docker Desktop, finish any WSL/reboot prompt, then run this bootstrap again."
        }
        Write-Info "Installing Docker Desktop through winget (Docker may request UAC or a restart)..."
        & $winget.Source install --exact --id Docker.DockerDesktop --source winget --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { Stop-Bootstrap "Docker Desktop installation failed (exit $LASTEXITCODE)." }
        Sync-ProcessPath
        $docker = Get-DockerCommand
    }

    if (-not $docker) {
        Stop-Bootstrap "Docker CLI is unavailable. Finish Docker Desktop installation, restart the shell if needed, then run this bootstrap again."
    }

    Write-Info "Starting Docker Desktop..."
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $docker desktop start *> $null
        $startExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($startExit -ne 0 -and -not (Test-DockerReady)) {
        Stop-Bootstrap "Docker Desktop did not start. Complete its WSL/UAC/reboot step, then run this same bootstrap again."
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($DockerTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-DockerReady) {
            Write-Ok "Docker Desktop is ready"
            return
        }
        Start-Sleep -Seconds 2
    }
    Stop-Bootstrap "Docker Desktop is not ready yet. It may need WSL initialization or a reboot; finish that step and run this same bootstrap again."
}

function Ensure-DockerCompose {
    $docker = Get-DockerCommand
    if (-not $docker) { Stop-Bootstrap "Docker CLI is unavailable." }
    & $docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Bootstrap "Docker Compose v2 is required. Update Docker Desktop, then run this bootstrap again."
    }
    return $docker
}

function Get-PowerShellHost {
    $psCommand = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if (-not $psCommand) { $psCommand = Get-Command pwsh.exe -ErrorAction SilentlyContinue }
    if (-not $psCommand) { $psCommand = Get-Command pwsh -ErrorAction SilentlyContinue }
    if (-not $psCommand) { Stop-Bootstrap "No PowerShell executable was found." }
    return $psCommand.Source
}

function Protect-SecretFile([string]$Path) {
    $icacls = Get-Command icacls.exe -ErrorAction SilentlyContinue
    if (-not $icacls) { $icacls = Get-Command icacls -ErrorAction SilentlyContinue }
    if (-not $icacls) { Stop-Bootstrap "icacls is unavailable; cannot protect $Path." }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $grant = "{0}:(F)" -f $identity
    & $icacls.Source $Path "/inheritance:r" "/grant:r" $grant "/grant:r" "SYSTEM:(F)" *> $null
    if ($LASTEXITCODE -ne 0) { Stop-Bootstrap "Could not restrict access to $Path." }
}

function New-CloakToken {
    $bytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes)
    } finally {
        $random.Dispose()
    }
}

function Invoke-DockerCompose([string]$Docker, [string[]]$BaseArguments, [string[]]$Arguments, [string]$Description) {
    Write-Info $Description
    & $Docker compose @BaseArguments @Arguments
    if ($LASTEXITCODE -ne 0) { Stop-Bootstrap "$Description failed (exit $LASTEXITCODE)." }
}

function Wait-ForDashboard([int]$Port) {
    $dashboardUrl = "http://127.0.0.1:$Port/cloak"
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { return $dashboardUrl }
        } catch {
            $errorResponse = $_.Exception.Response
            if ($errorResponse -and [int]$errorResponse.StatusCode -eq 401) {
                return $dashboardUrl
            }
            Start-Sleep -Seconds 2
        }
    }
    Stop-Bootstrap "Dashboard did not become ready at $dashboardUrl. Run 'docker compose --project-name $ComposeProjectName ps' in $InstallDir for logs."
}

function Install-NativeMode([string]$ResolvedHermesHome, [string]$ResolvedInstallDir) {
    $temporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-cloak-bootstrap-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporaryDirectory -Force | Out-Null

    try {
        $coreInstaller = Join-Path $temporaryDirectory "install.ps1"
        $coreUrl = "$RawRepository/$Branch/scripts/install.ps1"
        Write-Info "Downloading the installer entrypoint for branch '$Branch'..."
        Invoke-WebRequest -Uri $coreUrl -OutFile $coreInstaller -UseBasicParsing

        $env:HERMES_HOME = $ResolvedHermesHome
        $psHost = Get-PowerShellHost
        Write-Info "Installing Hermes and its Node driver (local Chromium is intentionally skipped)..."
        & $psHost -NoProfile -ExecutionPolicy Bypass -File $coreInstaller `
            -NonInteractive -SkipSetup -SkipBrowser -Branch $Branch `
            -HermesHome $ResolvedHermesHome -InstallDir $ResolvedInstallDir
        if ($LASTEXITCODE -ne 0) { Stop-Bootstrap "Hermes installation failed (exit $LASTEXITCODE)." }

        $cloakInstaller = Join-Path $ResolvedInstallDir "scripts\install_cloak.ps1"
        if (-not (Test-Path -LiteralPath $cloakInstaller)) {
            Stop-Bootstrap "Cloak installer was not found in $ResolvedInstallDir."
        }

        Write-Info "Provisioning CloakBrowser-Manager, protected CDP bridge, and provider config..."
        & $psHost -NoProfile -ExecutionPolicy Bypass -File $cloakInstaller `
            -ConfigureProvider -Strict -HermesHome $ResolvedHermesHome
        if ($LASTEXITCODE -ne 0) { Stop-Bootstrap "Cloak provisioning failed (exit $LASTEXITCODE)." }

        Write-Ok "Ready. Add your already-issued model credential only to $ResolvedHermesHome\.env before starting Hermes."
    } finally {
        Remove-Item -LiteralPath $temporaryDirectory -Force -Recurse -ErrorAction SilentlyContinue
    }
}

function Install-ComposeMode([string]$ResolvedInstallDir, [string]$ProjectName) {
    if (Test-Path -LiteralPath $ResolvedInstallDir) {
        $entries = @(Get-ChildItem -LiteralPath $ResolvedInstallDir -Force -ErrorAction Stop)
        if ($entries.Count -gt 0) {
            Stop-Bootstrap "Refusing to overwrite existing directory $ResolvedInstallDir. Choose an empty -InstallDir or remove it yourself after confirming its contents."
        }
    } else {
        New-Item -ItemType Directory -Path $ResolvedInstallDir -Force | Out-Null
    }

    $temporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-cloak-compose-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporaryDirectory -Force | Out-Null

    try {
        $archiveFile = Join-Path $temporaryDirectory "source.zip"
        $expandedDirectory = Join-Path $temporaryDirectory "source"
        $archiveUrl = "$ArchiveRepository/archive/refs/heads/$Branch.zip"
        Write-Info "Downloading Cloak Edition source for branch '$Branch'..."
        Invoke-WebRequest -Uri $archiveUrl -OutFile $archiveFile -UseBasicParsing
        Expand-Archive -LiteralPath $archiveFile -DestinationPath $expandedDirectory -Force

        $sourceRoots = @(Get-ChildItem -LiteralPath $expandedDirectory -Directory)
        if ($sourceRoots.Count -ne 1) { Stop-Bootstrap "Downloaded archive has an unexpected layout." }
        Get-ChildItem -LiteralPath $sourceRoots[0].FullName -Force | Copy-Item -Destination $ResolvedInstallDir -Recurse -Force

        $composeFile = Join-Path $ResolvedInstallDir "docker-compose.cloak.yml"
        if (-not (Test-Path -LiteralPath $composeFile)) {
            Stop-Bootstrap "Full Compose definition was not found in the downloaded source."
        }

        $composeEnvFile = Join-Path $ResolvedInstallDir ".cloak-compose.env"
        $composeEnvironment = @(
            "CLOAK_AUTH_TOKEN=$(New-CloakToken)",
            "CLOAK_MANAGER_PORT=8180",
            "CLOAK_MANAGER_BROWSER_URL=http://127.0.0.1:8180",
            "CLOAK_CDP_BRIDGE_PORT=8081",
            "HERMES_DASHBOARD_PORT=9119",
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin",
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=$(New-CloakToken)",
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET=$(New-CloakToken)",
            "HERMES_CLOAK_IMAGE=$ProjectName-hermes:local"
        )
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllLines($composeEnvFile, $composeEnvironment, $utf8NoBom)
        Protect-SecretFile $composeEnvFile

        $docker = Ensure-DockerCompose
        $baseArguments = @("--project-name", $ProjectName, "--env-file", $composeEnvFile, "-f", $composeFile)
        Invoke-DockerCompose $docker $baseArguments @("build", "hermes") "Building the isolated Hermes Cloak image (first run can take 15-45 minutes)"
        Invoke-DockerCompose $docker $baseArguments @("up", "-d", "--remove-orphans") "Starting Hermes (gateway + dashboard), CDP bridge and CloakBrowser Manager"
        Invoke-DockerCompose $docker $baseArguments @("exec", "-T", "hermes", "python", "scripts/cloak/bridge_readiness.py", "--manager-url", "http://manager:8080", "--bridge-url", "http://bridge:8081", "--timeout", "20") "Verifying the Manager-owned CDP path"
        $dashboardUrl = Wait-ForDashboard 9119
        Write-Ok "Ready. Dashboard: $dashboardUrl"
        Write-Info "Dashboard credentials are stored only in $composeEnvFile."
        Write-Host "[cloak-bootstrap] Stop later: docker compose --project-name $ProjectName --env-file `"$composeEnvFile`" -f `"$composeFile`" down" -ForegroundColor Yellow
    } finally {
        Remove-Item -LiteralPath $temporaryDirectory -Force -Recurse -ErrorAction SilentlyContinue
    }
}

if (-not (Test-SafeBranch $Branch)) { Stop-Bootstrap "Unsafe branch name." }
$resolvedMode = Resolve-InstallMode $Mode
Ensure-DockerDesktop

if ([string]::IsNullOrWhiteSpace($HermesHome)) {
    $HermesHome = if ($resolvedMode -eq "compose") { Join-Path $env:LOCALAPPDATA "hermes-compose" } elseif ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" }
}
if ([string]::IsNullOrWhiteSpace($InstallDir)) { $InstallDir = Join-Path $HermesHome "hermes-agent" }

if ($resolvedMode -eq "compose") {
    Install-ComposeMode $InstallDir $ComposeProjectName
} else {
    Install-NativeMode $HermesHome $InstallDir
}
