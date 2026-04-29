$ErrorActionPreference = "Stop"

# BLOCK 1: Keep update-only paths and logs in one small configuration block.
# VARS: ROOT = repository root, STATE_PATH = optional run.bat PID state, LOG_PATH = local update log
# WHY: update.bat should not become a second app launcher; it only updates source and local dependencies.
$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$USER_DIR = Join-Path $ROOT "user"
$LOG_DIR = Join-Path $USER_DIR "logs\runtime"
$STATE_PATH = Join-Path $USER_DIR "runtime\pids\vysol-run-state.json"
$RUN_ID = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$LOG_PATH = Join-Path $LOG_DIR "update-$RUN_ID.log"

# BLOCK 2: Create only the ignored runtime log folder used by the updater.
# WHY: Update diagnostics belong on the local machine, not in tracked project files or saved world folders.
New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null

function Write-UpdateLog {
  param(
    [string]$Message,
    [string]$Level = "INFO"
  )

  # BLOCK 3: Print readable update messages and mirror them into a local log.
  # WHY: Git and dependency failures need visible context without touching user data or tracked files.
  $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString("o"), $Level, $Message
  Add-Content -LiteralPath $LOG_PATH -Value $line
  Write-Host $Message
}

function Invoke-CheckedCommand {
  param(
    [string]$Description,
    [string]$FilePath,
    [string[]]$ArgumentList,
    [string]$WorkingDirectory = $ROOT
  )

  # BLOCK 4: Run one updater command and stop immediately if it fails.
  # WHY: A failed fetch, pull, or dependency sync should not be hidden behind later commands.
  Write-UpdateLog $Description
  Push-Location $WorkingDirectory
  try {
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
      throw "$Description failed with exit code $LASTEXITCODE."
    }
  }
  finally {
    Pop-Location
  }
}

function Get-ProcessInfo {
  param([int]$ProcessIdValue)

  # BLOCK 5: Read process command lines for recorded launcher PIDs.
  # WHY: The updater may warn about a running app, but it should never stop arbitrary processes.
  return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessIdValue" -ErrorAction SilentlyContinue
}

function Test-AppOwnedProcess {
  param([int]$ProcessIdValue)

  # BLOCK 6: Confirm a recorded PID still points at this checkout before treating the app as running.
  # WHY: Windows can reuse PIDs, so update.bat must not block on unrelated processes that inherited an old number.
  $processInfo = Get-ProcessInfo -ProcessIdValue $ProcessIdValue
  if ($null -eq $processInfo -or [string]::IsNullOrWhiteSpace($processInfo.CommandLine)) {
    return $false
  }
  $commandLine = $processInfo.CommandLine.Replace("/", "\")
  return $commandLine.IndexOf($ROOT.TrimEnd("\"), [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Assert-AppNotRunning {
  # BLOCK 7: Refuse to update source files while a recorded app run is still alive.
  # WHY: Updating under a running dev server can leave the user with a mixed old/new runtime; the updater should ask them to stop run.bat first instead of killing it.
  if (-not (Test-Path -LiteralPath $STATE_PATH)) {
    return
  }
  $state = Get-Content -Raw -LiteralPath $STATE_PATH | ConvertFrom-Json
  if ($state.root -and (([string]$state.root).TrimEnd("\") -ne $ROOT.TrimEnd("\"))) {
    return
  }
  foreach ($property in @("backend_pid", "frontend_pid", "neo4j_pid")) {
    if ($state.PSObject.Properties.Name -contains $property -and $state.$property) {
      $ProcessIdValue = [int]$state.$property
      if (Test-AppOwnedProcess -ProcessIdValue $ProcessIdValue) {
        throw "VySol appears to be running from a previous run.bat session. Stop the run.bat window before updating."
      }
    }
  }
}

function Assert-UpdateToolsAvailable {
  # BLOCK 8: Verify the updater's external tools before touching Git state.
  # WHY: Missing Git or npm should produce a readable setup error instead of a partial update attempt.
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git was not found on PATH. Install Git from https://git-scm.com/downloads and run update.bat again."
  }
  $frontendDir = Join-Path $ROOT "frontend"
  $nodeModules = Join-Path $frontendDir "node_modules"
  if ((Test-Path -LiteralPath $nodeModules) -and -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "npm.cmd was not found on PATH, so existing frontend dependencies cannot be synced. Reinstall Node.js and run update.bat again."
  }
}

function Assert-CleanTrackedWorktree {
  # BLOCK 9: Stop before updating when tracked local edits exist.
  # WHY: Fast-forward pulls are safe only when local tracked work will not be overwritten or mixed with incoming changes.
  & git diff --quiet --ignore-submodules --
  if ($LASTEXITCODE -ne 0) {
    throw "Working tree has tracked changes. Commit, stash, or discard them before updating."
  }
  & git diff --cached --quiet --ignore-submodules --
  if ($LASTEXITCODE -ne 0) {
    throw "Working tree has staged tracked changes. Commit or unstage them before updating."
  }
}

function Sync-PythonDependenciesIfPresent {
  # BLOCK 10: Sync Python dependencies only when the local venv already exists.
  # WHY: update.bat should not become first-run setup; run.bat owns venv creation and full startup readiness.
  $pythonExe = Join-Path $ROOT "venv\Scripts\python.exe"
  $requirements = Join-Path $ROOT "requirements.txt"
  if ((Test-Path -LiteralPath $pythonExe) -and (Test-Path -LiteralPath $requirements)) {
    Invoke-CheckedCommand -Description "Syncing Python dependencies..." -FilePath $pythonExe -ArgumentList @("-m", "pip", "install", "-r", "requirements.txt")
  }
}

function Sync-FrontendDependenciesIfPresent {
  # BLOCK 11: Sync frontend dependencies only when the local frontend install already exists.
  # WHY: Updating package-lock.json should refresh an existing dev install without making update.bat responsible for first setup.
  $frontendDir = Join-Path $ROOT "frontend"
  $nodeModules = Join-Path $frontendDir "node_modules"
  $packageJson = Join-Path $frontendDir "package.json"
  if ((Test-Path -LiteralPath $nodeModules) -and (Test-Path -LiteralPath $packageJson)) {
    Invoke-CheckedCommand -Description "Syncing frontend dependencies..." -FilePath "npm.cmd" -ArgumentList @("install") -WorkingDirectory $frontendDir
  }
}

try {
  # BLOCK 12: Apply the update without starting, stopping, deleting, or moving user data.
  # WHY: The current update.bat contract is update-only, so app startup behavior belongs in run.bat after the update is complete.
  Push-Location $ROOT
  Assert-AppNotRunning
  Assert-UpdateToolsAvailable
  Assert-CleanTrackedWorktree
  Invoke-CheckedCommand -Description "Fetching updates from GitHub..." -FilePath "git" -ArgumentList @("fetch", "--all", "--prune")
  Invoke-CheckedCommand -Description "Applying the latest fast-forward update..." -FilePath "git" -ArgumentList @("pull", "--ff-only")
  Sync-PythonDependenciesIfPresent
  Sync-FrontendDependenciesIfPresent
  Write-UpdateLog "Update complete."
  exit 0
}
catch {
  Write-UpdateLog $_.Exception.Message "ERROR"
  Write-Host ""
  Write-Host "Update failed. See update log:"
  Write-Host $LOG_PATH
  exit 1
}
finally {
  Pop-Location
}
