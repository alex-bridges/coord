<#
.SYNOPSIS
Install (or remove) the coord conductor watcher on Windows using Task Scheduler.

.DESCRIPTION
Registers a per-user Scheduled Task that runs `coord conductor tick` every N seconds from
a checkout kept detached at `origin/main` (default: ~\conductor\<name>).

.EXAMPLE
.\conductor\install.ps1 -Repo owner/name
.\conductor\install.ps1 -Repo owner/name -Interval 300 -NoWake
.\conductor\install.ps1 -Repo owner/name -Uninstall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [string]$HomeDir = "",

    [int]$Interval = 300,

    [switch]$NoWake,

    [switch]$Wake,

    [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "install.ps1: -Repo owner/name is required"
}
if ($NoWake -and $Wake) {
    throw "install.ps1: pass either -NoWake or -Wake, not both"
}
if ($Interval -lt 60) {
    throw "install.ps1: -Interval must be at least 60 seconds for Windows Task Scheduler"
}

$RepoName = ($Repo -split '/', 2)[1]
$RepoSlug = $Repo -replace '/', '-'
$TaskName = "coord-conductor-$RepoSlug"
$LocalBase = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "coord" } else { Join-Path $HOME ".coord" }
$LogDir = Join-Path $LocalBase "logs"
$LogFile = Join-Path $LogDir "conductor-$RepoSlug.log"
$WrapperPath = Join-Path $LocalBase "conductor-$RepoSlug.ps1"
$MetaPath = Join-Path $LocalBase "conductor-$RepoSlug.json"
if (-not $HomeDir) {
    $HomeDir = Join-Path (Join-Path $HOME "conductor") $RepoName
}
$CoordScript = Join-Path (Split-Path -Parent $PSScriptRoot) "coord"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Remove-Item -Path $WrapperPath, $MetaPath -Force -ErrorAction SilentlyContinue
    Write-Host "removed $TaskName (checkout at $HomeDir and conductor state directory are kept)"
    exit 0
}

foreach ($tool in @("git", "python", "gh")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "install.ps1: $tool is not on PATH"
    }
}
$PythonExe = (Get-Command python).Source
if (-not (Test-Path $CoordScript)) {
    throw "install.ps1: cannot find coord next to this script ($CoordScript)"
}

& gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    throw "install.ps1: gh is not authenticated (run `gh auth login`)"
}

# Preserve previous wake mode on refresh unless explicitly overridden
$WakeMode = "wake"
if ($NoWake) {
    $WakeMode = "no-wake"
} elseif ($Wake) {
    $WakeMode = "wake"
} elseif (Test-Path $MetaPath) {
    try {
        $prev = Get-Content $MetaPath -Raw | ConvertFrom-Json
        if ($prev.wakeMode -in @("wake", "no-wake")) { $WakeMode = $prev.wakeMode }
    } catch { }
}

if ($WakeMode -eq "wake") {
    $claudeCmd = if ($env:CONDUCTOR_CLAUDE) { $env:CONDUCTOR_CLAUDE } else { "claude" }
    if (-not (Get-Command $claudeCmd -ErrorAction SilentlyContinue)) {
        throw "install.ps1: $claudeCmd is not on PATH (use -NoWake for model-free ticks)"
    }
}

if (-not (Test-Path (Join-Path $HomeDir ".git"))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $HomeDir) -Force | Out-Null
    & gh repo clone $Repo $HomeDir -- -q
    if ($LASTEXITCODE -ne 0) { throw "install.ps1: failed to clone $Repo" }
    & git -C $HomeDir checkout -q --detach origin/main
    Write-Host "created conductor checkout: $HomeDir (detached at origin/main)"
}

$HostName = $env:COMPUTERNAME
if (-not $HostName) { $HostName = [System.Net.Dns]::GetHostName() }
$Agent = "conductor@${HostName}:conductor"

# Bring a clean detached checkout up to origin/main before dry-run check
$dirty = & git -C $HomeDir status --porcelain
if ($dirty) {
    Write-Warning "$HomeDir has local changes; checking it as-is"
} else {
    & git -C $HomeDir symbolic-ref -q HEAD *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Warning "$HomeDir is on a branch; watcher expects it detached at origin/main"
    } else {
        & git -C $HomeDir fetch -q origin main
        if ($LASTEXITCODE -eq 0) {
            & git -C $HomeDir checkout -q --detach FETCH_HEAD
        }
    }
}

$shortHead = (& git -C $HomeDir rev-parse --short HEAD).Trim()
Write-Host "checking dry-run tick as $Agent at $shortHead ..."

$tickArgs = @($CoordScript, "conductor", "tick")
if ($WakeMode -eq "no-wake") { $tickArgs += "--no-wake" }

$savedEnv = @{
    COORD_AGENT = $env:COORD_AGENT
    CONDUCTOR_HOME = $env:CONDUCTOR_HOME
    COORD_REPO = $env:COORD_REPO
}
try {
    $env:COORD_AGENT = $Agent
    $env:CONDUCTOR_HOME = $HomeDir
    $env:COORD_REPO = $Repo
    Push-Location $HomeDir
    $dryOut = & $PythonExe @($tickArgs + @("--dry-run")) 2>&1
    $dryCode = $LASTEXITCODE
} finally {
    Pop-Location
    foreach ($k in $savedEnv.Keys) {
        if ($null -eq $savedEnv[$k]) { Remove-Item "Env:$k" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$k" $savedEnv[$k] }
    }
}
$dryText = ($dryOut | Out-String).Trim()
if ($dryText) { Write-Host $dryText }
if ($dryCode -ne 0) {
    throw "install.ps1: dry-run tick failed (exit $dryCode); task not updated"
}
if ($dryText -match "another tick is running") {
    throw "install.ps1: a tick is running; try again when it ends"
}

New-Item -ItemType Directory -Path $LocalBase, $LogDir -Force | Out-Null

# Write launcher script that sets environment, runs in checkout, and appends to log
$extraFlag = if ($WakeMode -eq "no-wake") { " '--no-wake'" } else { "" }
$wrapperContent = @"
`$ErrorActionPreference = 'Continue'
`$env:COORD_AGENT = '$($Agent -replace "'", "''")'
`$env:CONDUCTOR_HOME = '$($HomeDir -replace "'", "''")'
`$env:COORD_REPO = '$($Repo -replace "'", "''")'
Set-Location '$($HomeDir -replace "'", "''")'
& '$($PythonExe -replace "'", "''")' '$($CoordScript -replace "'", "''")' 'conductor' 'tick'$extraFlag *>> '$($LogFile -replace "'", "''")'
"@
Set-Content -Path $WrapperPath -Value $wrapperContent -Encoding UTF8

@{
    repo = $Repo
    homeDir = $HomeDir
    interval = $Interval
    wakeMode = $WakeMode
    agent = $Agent
    logFile = $LogFile
} | ConvertTo-Json | Set-Content -Path $MetaPath -Encoding UTF8

$minutes = [Math]::Max(1, [int][Math]::Ceiling($Interval / 60.0))
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$WrapperPath`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $minutes) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "coord conductor watcher for $Repo" -Force | Out-Null
Write-Host "installed $TaskName: every ${minutes}m ($WakeMode), log $LogFile"
Write-Host "status: coord conductor status   stop: .\conductor\install.ps1 -Repo $Repo -Uninstall"
