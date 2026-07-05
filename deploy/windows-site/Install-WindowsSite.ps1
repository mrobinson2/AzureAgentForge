#Requires -Version 5.1
<#
.SYNOPSIS
  Install the Windows PRIMARY site host automation — the Windows/WSL2 equivalent
  of the mac-site launchd LaunchAgents. Run from an ELEVATED (admin) Windows
  PowerShell.

.DESCRIPTION
  The containers and bash automation are cross-platform: the Linux stack and the
  POSIX scripts under deploy/mac-site/ run UNCHANGED inside WSL2 (Ubuntu). This
  script installs the Windows-specific overlay only:

    1. Verifies WSL2, the target distro, and Docker reachability from WSL.
    2. Adjusts power settings so the always-on site stays up (no sleep/hibernate
       on AC power).
    3. Imports the five Task Scheduler tasks from tasks/*.xml — the Windows
       equivalent of the five macOS LaunchAgents — substituting {{REPO}},
       {{WSL_DISTRO}}, {{WSL_USER}} with resolved values. Idempotent (-Force).

  It does NOT bring the stack up or pull secrets — those are the shared WSL steps
  printed in the "next steps" summary. Nothing under deploy/mac-site/ or scripts/
  is modified.

.PARAMETER RepoPathWsl
  Path to the repo checkout INSIDE WSL. Default: ~/Code/AzureAgentForge
  Resolved to an absolute path via WSL before it is baked into the tasks.

.PARAMETER WslDistro
  WSL distro name. Default: Ubuntu (confirm with `wsl -l -v`).

.PARAMETER WslUser
  WSL username. Default: auto-detected via `wsl -d <distro> -e whoami`.

.EXAMPLE
  # From an elevated Windows PowerShell:
  .\Install-WindowsSite.ps1
  .\Install-WindowsSite.ps1 -WslDistro Ubuntu-22.04 -RepoPathWsl '~/src/AzureAgentForge'
#>
[CmdletBinding()]
param(
  [string]$RepoPathWsl = '~/Code/AzureAgentForge',
  [string]$WslDistro   = 'Ubuntu',
  [string]$WslUser     = ''
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$tasksDir  = Join-Path $scriptDir 'tasks'

function Info($m) { Write-Host "[i] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ── admin check ─────────────────────────────────────────────────────────────
$isAdmin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
  Die "This script must run elevated (Run as administrator) — it edits power settings and registers scheduled tasks."
}

# ── verify WSL2 (soft-fail: warn + continue) ────────────────────────────────
Info "Checking WSL..."
try {
  $null = wsl.exe --status 2>&1
  if ($LASTEXITCODE -ne 0) { Warn "``wsl --status`` returned $LASTEXITCODE — is WSL2 installed? Continuing." }
  else { Ok "WSL present." }
} catch { Warn "Could not run ``wsl --status`` ($_). Continuing." }

# ── verify the distro exists ────────────────────────────────────────────────
Info "Checking distro '$WslDistro'..."
# wsl -l output is UTF-16; normalize before matching.
$distros = (wsl.exe -l -q 2>$null) -replace "`0", '' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if ($distros -notcontains $WslDistro) {
  Warn "Distro '$WslDistro' not found in: $($distros -join ', '). Confirm with 'wsl -l -v'. Continuing (tasks will still be written)."
} else {
  Ok "Distro '$WslDistro' found."
}

# ── resolve the WSL user ────────────────────────────────────────────────────
if ([string]::IsNullOrWhiteSpace($WslUser)) {
  try {
    $WslUser = (wsl.exe -d $WslDistro -e whoami 2>$null | Out-String).Trim()
  } catch { }
  if ([string]::IsNullOrWhiteSpace($WslUser)) {
    Die "Could not auto-detect the WSL user. Pass -WslUser explicitly."
  }
  Ok "Detected WSL user: $WslUser"
} else {
  Info "Using WSL user: $WslUser"
}

# ── resolve the repo path to an absolute WSL path (tilde won't expand in the
#    double-quoted `bash -lc` argument, so bake in the absolute path) ─────────
$repoAbs = ''
try {
  $repoAbs = (wsl.exe -d $WslDistro -u $WslUser -e bash -lc "cd '$RepoPathWsl' && pwd" 2>$null | Out-String).Trim()
} catch { }
if ([string]::IsNullOrWhiteSpace($repoAbs)) {
  # Fallback: expand a leading ~ manually.
  $repoAbs = $RepoPathWsl -replace '^~', "/home/$WslUser"
  Warn "Could not resolve '$RepoPathWsl' via WSL; falling back to '$repoAbs'. Confirm the repo is cloned there."
} else {
  Ok "Repo resolved in WSL: $repoAbs"
}

# ── verify Docker reachable from WSL (soft-fail) ────────────────────────────
Info "Checking Docker from WSL..."
try {
  $null = wsl.exe -d $WslDistro -u $WslUser -e docker version 2>$null
  if ($LASTEXITCODE -eq 0) { Ok "Docker reachable from WSL." }
  else { Warn "``docker version`` failed in WSL ($LASTEXITCODE). Enable Docker Desktop's WSL integration for '$WslDistro'. Continuing." }
} catch { Warn "Could not run docker in WSL ($_). Continuing." }

# ── power settings: keep the always-on site awake on AC ─────────────────────
# This CHANGES Windows power settings on AC power so the host does not sleep or
# hibernate (the site must stay up). Battery settings are left untouched.
Info "Setting AC power: standby-timeout-ac 0 and hibernate-timeout-ac 0 (never sleep/hibernate on AC)..."
try {
  powercfg /change standby-timeout-ac 0
  powercfg /change hibernate-timeout-ac 0
  Ok "AC sleep + hibernate disabled. (Reverse later with ``powercfg /change standby-timeout-ac <minutes>``.)"
} catch { Warn "powercfg failed ($_). Set 'Sleep = Never (plugged in)' manually in Power & sleep settings." }

# ── import the five Task Scheduler tasks ────────────────────────────────────
$tasks = @(
  'AAF-BootGuard',
  'AAF-ImagePull',
  'AAF-PgFirewall',
  'AAF-ShareSync',
  'AAF-SkillCurator'
)

foreach ($t in $tasks) {
  $xmlPath = Join-Path $tasksDir "$t.xml"
  if (-not (Test-Path $xmlPath)) { Warn "Missing $xmlPath — skipping $t."; continue }

  $xml = Get-Content -Raw -Path $xmlPath
  $xml = $xml.Replace('{{REPO}}',       $repoAbs)
  $xml = $xml.Replace('{{WSL_DISTRO}}', $WslDistro)
  $xml = $xml.Replace('{{WSL_USER}}',   $WslUser)

  try {
    Register-ScheduledTask -TaskName $t -TaskPath '\AzureAgentForge\' -Xml $xml -Force `
      -User "$env:USERDOMAIN\$env:USERNAME" | Out-Null
    Ok "Registered \AzureAgentForge\$t"
  } catch {
    Warn "Failed to register $t : $_"
  }
}

# ── next steps ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Next steps (run INSIDE WSL '$WslDistro' as '$WslUser') ===" -ForegroundColor Magenta
Write-Host @"
  1. az login                         # operator identity (KV Secrets User, Storage File
                                       #   Data Privileged Contributor, PG/Automation)
  2. azcopy login                     # for the hourly share sync (writes to Azure Files)
  3. cd $repoAbs/deploy/mac-site
     cp .env.example .env             # then set the [you] values
     #  -> AZURE_PG_HOST, AAF_HOME=/home/$WslUser/aaf, and the amd64 image tags
     #     (e.g. HONCHO_IMAGE_TAG=100 — NOT 100-arm64). See
     #     deploy/windows-site/.env.example for the two Windows overrides.
  4. ./secrets-pull.sh                # writes secret values from Key Vault into .env
  5. cd $repoAbs && ./scripts/aaf-site local   # take the lease + bring the stack up
                                       #   (needs zsh: `sudo apt install zsh`)
  6. ./scripts/aaf-site status        # verify lease + containers + cloud standby

  The five scheduled tasks are installed under Task Scheduler > \AzureAgentForge\
  and will drive host automation exactly like the mac-site launchd agents. Logs
  land in WSL at ~/aaf/logs (boot-guard.log, image-pull.log, pg-firewall.log,
  sync + the cloud sync freshness event).
"@ -ForegroundColor Gray
Ok "Install-WindowsSite complete."
