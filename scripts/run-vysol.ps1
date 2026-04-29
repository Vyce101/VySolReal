param(
  [switch]$SkipStart
)

$ErrorActionPreference = "Stop"

# BLOCK 1: Keep the launcher contract in one visible configuration block.
# VARS: *_PORT = local development ports, *_URL = readiness checks and browser target, ROOT = repository root
# WHY: The launcher owns startup coordination, so final commands, ports, health checks, logs, and PID files should not be scattered through batch labels.
$ROOT = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$MIN_PYTHON_VERSION = [version]"3.14.0"
$TESTED_PYTHON_FAMILY = "3.14"
$MIN_NODE_VERSION = [version]"20.19.0"
$TESTED_NODE_MAJOR = "24"
$BACKEND_PORT = 8000
$FRONTEND_PORT = 5173
$BACKEND_HEALTH_URL = "http://127.0.0.1:$BACKEND_PORT/api/health"
$FRONTEND_URL = "http://127.0.0.1:$FRONTEND_PORT/"
$VENV_DIR = Join-Path $ROOT "venv"
$PYTHON_EXE = Join-Path $VENV_DIR "Scripts\python.exe"
$REQ_STAMP = Join-Path $VENV_DIR ".requirements.stamp"
$FRONTEND_DIR = Join-Path $ROOT "frontend"
$FRONTEND_STAMP = Join-Path $FRONTEND_DIR "node_modules\.package-lock.stamp"
$USER_DIR = Join-Path $ROOT "user"
$RUNTIME_DIR = Join-Path $USER_DIR "runtime"
$PID_DIR = Join-Path $RUNTIME_DIR "pids"
$LOG_DIR = Join-Path $USER_DIR "logs\runtime"
$RUN_ID = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$STATE_PATH = Join-Path $PID_DIR "vysol-run-state.json"
$NEO4J_PID_PATH = Join-Path $PID_DIR "neo4j.pid"
$LAUNCHER_LOG = Join-Path $LOG_DIR "launcher-$RUN_ID.log"
$BACKEND_LOG = Join-Path $LOG_DIR "backend-$RUN_ID.log"
$BACKEND_ERROR_LOG = Join-Path $LOG_DIR "backend-$RUN_ID.err.log"
$FRONTEND_LOG = Join-Path $LOG_DIR "frontend-$RUN_ID.log"
$FRONTEND_ERROR_LOG = Join-Path $LOG_DIR "frontend-$RUN_ID.err.log"
$script:PythonLauncher = $null
$script:PythonSelector = @()
$script:PythonLabel = $null
$script:PythonVersion = $null
$script:NodeVersion = $null
$script:StartedState = $null
$script:StartedRootPids = @()

# BLOCK 2: Create only ignored runtime folders used by launcher logs and PID state.
# WHY: Startup needs local diagnostics and restart cleanup, but those files must stay out of tracked source and away from saved world data.
New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
New-Item -ItemType Directory -Path $PID_DIR -Force | Out-Null

function Write-LauncherLog {
  param(
    [string]$Message,
    [string]$Level = "INFO"
  )

  # BLOCK 3: Print readable launcher messages and mirror them into a local runtime log.
  # VARS: line = timestamped log row written to user/logs/runtime
  # WHY: Startup failures need enough detail to diagnose without mixing secrets or local state into tracked files.
  $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString("o"), $Level, $Message
  Add-Content -LiteralPath $LAUNCHER_LOG -Value $line
  Write-Host $Message
}

function Invoke-CheckedCommand {
  param(
    [string]$Description,
    [string]$FilePath,
    [string[]]$ArgumentList,
    [string]$WorkingDirectory = $ROOT
  )

  # BLOCK 4: Run one setup command and stop immediately if it fails.
  # WHY: Dependency setup is safer when the first failing command reports a clear stage instead of cascading into startup timeouts.
  Write-LauncherLog $Description
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

function Get-ExternalVersion {
  param([string]$VersionText)

  # BLOCK 5: Extract a plain semantic version from command output.
  # WHY: Python and Node print slightly different labels, but version comparison should use the numeric version only.
  if ($VersionText -match "([0-9]+(?:\.[0-9]+){1,3})") {
    return [version]$Matches[1]
  }
  throw "Could not read a version from '$VersionText'."
}

function Initialize-Python {
  # BLOCK 6: Prefer the Windows Python launcher, then fall back to python on PATH.
  # WHY: `py -3` normally finds the newest installed Python 3 on Windows while still allowing simpler PATH-only installs.
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $versionText = & py -3 --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $versionText) {
      $script:PythonLauncher = "py"
      $script:PythonSelector = @("-3")
      $script:PythonLabel = "py -3"
      $script:PythonVersion = Get-ExternalVersion $versionText
    }
  }

  if (-not $script:PythonVersion -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $versionText = & python --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $versionText) {
      $script:PythonLauncher = "python"
      $script:PythonSelector = @()
      $script:PythonLabel = "python"
      $script:PythonVersion = Get-ExternalVersion $versionText
    }
  }

  # BLOCK 7: Fail before setup when no compatible Python is available.
  # WHY: Creating a venv or installing dependencies with the wrong interpreter would leave confusing partial local state.
  if (-not $script:PythonVersion) {
    throw "Python 3.14 or newer is required. Install Python from https://www.python.org/downloads/windows/ and run run.bat again."
  }
  if ($script:PythonVersion -lt $MIN_PYTHON_VERSION) {
    throw "Found Python $script:PythonVersion, but VySol requires at least Python $MIN_PYTHON_VERSION."
  }
  $pythonFamily = "{0}.{1}" -f $script:PythonVersion.Major, $script:PythonVersion.Minor
  if ($pythonFamily -ne $TESTED_PYTHON_FAMILY) {
    Write-LauncherLog "Warning: VySol is developed against Python $TESTED_PYTHON_FAMILY.x, but found $script:PythonVersion." "WARNING"
  }
}

function Initialize-Node {
  # BLOCK 8: Verify Node and npm before any frontend setup runs.
  # WHY: Vite startup failures are much clearer when missing Node/npm is reported before dependency or port checks.
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js 20.19 or newer is required. Install Node.js from https://nodejs.org/en/download and run run.bat again."
  }
  if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "Node.js was found, but npm.cmd was not available on PATH. Reinstall Node.js and run run.bat again."
  }

  $script:NodeVersion = [version](& node -p "process.versions.node")
  if ($script:NodeVersion -lt $MIN_NODE_VERSION) {
    throw "Found Node.js $script:NodeVersion, but VySol requires at least Node.js $MIN_NODE_VERSION."
  }
  if ([string]$script:NodeVersion.Major -ne $TESTED_NODE_MAJOR) {
    Write-LauncherLog "Warning: VySol is developed against Node.js $TESTED_NODE_MAJOR.x, but found $script:NodeVersion." "WARNING"
  }
}

function Sync-PythonDependencies {
  # BLOCK 9: Create the virtual environment with the detected compatible Python.
  # WHY: All Python dependency work must stay inside the project venv instead of touching global Python packages.
  $firstSetup = $false
  if (-not (Test-Path -LiteralPath $PYTHON_EXE)) {
    Write-LauncherLog "Setting up virtual environment with $script:PythonLabel..."
    & $script:PythonLauncher @script:PythonSelector -m venv $VENV_DIR
    if ($LASTEXITCODE -ne 0) {
      throw "Failed to create the virtual environment."
    }
    $firstSetup = $true
  }

  # BLOCK 10: Install pinned Python dependencies only when the venv is new or requirements changed.
  # WHY: Reinstalling on every launch is slow, but stale dependencies can break newly updated backend modules.
  if ($firstSetup) {
    Invoke-CheckedCommand -Description "Upgrading pip inside the local venv..." -FilePath $PYTHON_EXE -ArgumentList @("-m", "pip", "install", "--upgrade", "pip==25.3")
  }
  if (Test-Path -LiteralPath (Join-Path $ROOT "requirements.txt")) {
    $requirements = Get-Item -LiteralPath (Join-Path $ROOT "requirements.txt")
    $stampIsMissing = -not (Test-Path -LiteralPath $REQ_STAMP)
    $stampIsStale = $stampIsMissing -or $requirements.LastWriteTimeUtc -gt (Get-Item -LiteralPath $REQ_STAMP).LastWriteTimeUtc
    if ($firstSetup -or $stampIsStale) {
      Invoke-CheckedCommand -Description "Installing pinned Python dependencies from requirements.txt..." -FilePath $PYTHON_EXE -ArgumentList @("-m", "pip", "install", "-r", "requirements.txt")
      Copy-Item -LiteralPath $requirements.FullName -Destination $REQ_STAMP -Force
    }
  }
}

function Sync-FrontendDependencies {
  # BLOCK 11: Install frontend dependencies only when the package lock is new or node_modules is missing.
  # WHY: The app should use pinned frontend packages without paying the install cost on every normal launch.
  $packageJson = Join-Path $FRONTEND_DIR "package.json"
  $packageLock = Join-Path $FRONTEND_DIR "package-lock.json"
  if (-not (Test-Path -LiteralPath $packageJson)) {
    throw "Frontend package.json was not found at $FRONTEND_DIR."
  }

  $nodeModulesMissing = -not (Test-Path -LiteralPath (Join-Path $FRONTEND_DIR "node_modules"))
  $lockExists = Test-Path -LiteralPath $packageLock
  $stampIsMissing = -not (Test-Path -LiteralPath $FRONTEND_STAMP)
  $stampIsStale = $lockExists -and ($stampIsMissing -or (Get-Item -LiteralPath $packageLock).LastWriteTimeUtc -gt (Get-Item -LiteralPath $FRONTEND_STAMP).LastWriteTimeUtc)
  if ($nodeModulesMissing -or $stampIsStale) {
    Invoke-CheckedCommand -Description "Installing pinned frontend dependencies..." -FilePath "npm.cmd" -ArgumentList @("install") -WorkingDirectory $FRONTEND_DIR
    if ($lockExists) {
      Copy-Item -LiteralPath $packageLock -Destination $FRONTEND_STAMP -Force
    }
  }
}

function Get-ProcessInfo {
  param([int]$ProcessIdValue)

  # BLOCK 12: Read process metadata through Windows process management.
  # WHY: PID ownership must be verified from the actual command line before the launcher stops anything.
  return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessIdValue" -ErrorAction SilentlyContinue
}

function Test-AppOwnedProcess {
  param([int]$ProcessIdValue)

  # BLOCK 13: Treat a process as app-owned only when its command line points at this checkout.
  # WHY: Executable names and framework commands are too generic; the checkout path is the safest proof before cleanup touches a process.
  if ($ProcessIdValue -eq $PID) {
    return $false
  }
  $processInfo = Get-ProcessInfo -ProcessIdValue $ProcessIdValue
  if ($null -eq $processInfo -or [string]::IsNullOrWhiteSpace($processInfo.CommandLine)) {
    return $false
  }
  $commandLine = $processInfo.CommandLine.Replace("/", "\")
  $normalizedRoot = $ROOT.TrimEnd("\")
  if ($commandLine.IndexOf($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
    return $true
  }
  return $false
}

function Get-ChildProcessIds {
  param([int]$ParentProcessId)

  # BLOCK 14: Walk the child process tree beneath one launcher-owned root process.
  # WHY: npm and cmd wrappers can spawn Node or Python children, so cleanup must stop the tree without scanning unrelated executables.
  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    $childId = [int]$child.ProcessId
    $childId
    Get-ChildProcessIds -ParentProcessId $childId
  }
}

function Stop-AppOwnedProcessTree {
  param(
    [int]$RootProcessId,
    [string]$Reason
  )

  # BLOCK 15: Stop only a recorded process tree after checking the root process belongs to this app.
  # WHY: PID reuse is possible on Windows, so stale PID cleanup must re-check ownership before calling Stop-Process.
  if (-not (Test-AppOwnedProcess -ProcessIdValue $RootProcessId)) {
    return
  }
  Write-LauncherLog "Stopping app-owned process tree rooted at PID $RootProcessId ($Reason)..."
  $processTree = @((Get-ChildProcessIds -ParentProcessId $RootProcessId) + $RootProcessId | Select-Object -Unique)
  [array]::Reverse($processTree)
  foreach ($ProcessIdValue in $processTree) {
    if ($ProcessIdValue -eq $PID) {
      continue
    }
    $process = Get-Process -Id $ProcessIdValue -ErrorAction SilentlyContinue
    if ($null -ne $process) {
      Stop-Process -Id $ProcessIdValue -Force -ErrorAction SilentlyContinue
    }
  }
}

function Get-RecordedRootPids {
  param([object]$State)

  # BLOCK 16: Read only known process fields from the launcher state file.
  # WHY: Runtime JSON should not become a generic command source; it only records PIDs and listener IDs that this launcher observed.
  $ids = @()
  foreach ($property in @("backend_pid", "frontend_pid", "neo4j_pid", "backend_listener_pids", "frontend_listener_pids")) {
    if ($State.PSObject.Properties.Name -contains $property -and $State.$property) {
      foreach ($ProcessIdValue in @($State.$property)) {
        $ids += [int]$ProcessIdValue
      }
    }
  }
  if ($State.PSObject.Properties.Name -contains "processes" -and $State.processes) {
    foreach ($processRecord in @($State.processes)) {
      if ($processRecord.PSObject.Properties.Name -contains "pid" -and $processRecord.pid) {
        $ids += [int]$processRecord.pid
      }
    }
  }
  return $ids | Select-Object -Unique
}

function Stop-RecordedAppProcesses {
  param(
    [string]$StatePath,
    [string]$Reason
  )

  # BLOCK 17: Clean up only prior launcher state from this same checkout.
  # WHY: Multiple local copies of the repo can exist, so one checkout must not stop another checkout's recorded processes.
  if (-not (Test-Path -LiteralPath $StatePath)) {
    return
  }
  $state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
  if ($state.root -and (([string]$state.root).TrimEnd("\") -ne $ROOT.TrimEnd("\"))) {
    return
  }
  foreach ($ProcessIdValue in (Get-RecordedRootPids -State $state)) {
    Stop-AppOwnedProcessTree -RootProcessId $ProcessIdValue -Reason $Reason
  }
}

function Get-PortOwners {
  param([int]$Port)

  # BLOCK 18: Find local listener PIDs for one required port.
  # WHY: A clear occupied-port error is safer than killing by executable name or letting Vite/Uvicorn fail later.
  $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
  return $connections | Select-Object -ExpandProperty OwningProcess -Unique
}

function Get-CommandLineFingerprint {
  param([string]$CommandLine)

  # BLOCK 19: Convert a command line into a stable fingerprint without saving the raw local path.
  # VARS: bytes = UTF-8 command text, hash = SHA-256 digest used as non-secret process evidence
  # WHY: Runtime state needs enough evidence to debug PID reuse, but raw command lines can contain machine-specific paths.
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($CommandLine)
  $sha256 = [System.Security.Cryptography.SHA256]::Create()
  try {
    $hash = $sha256.ComputeHash($bytes)
    return ([System.BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
  }
  finally {
    $sha256.Dispose()
  }
}

function Get-AppOwnedProcessEvidence {
  param(
    [int]$ProcessIdValue,
    [string]$Role,
    [int]$Port = 0
  )

  # BLOCK 20: Build safe runtime evidence for one process that already passed ownership checks.
  # VARS: processInfo = Windows process metadata, fingerprint = non-secret hash of the process command line
  # WHY: Recording listener PIDs and fingerprints lets the next launcher recover from terminal-close crashes without trusting stale PIDs alone.
  if (-not (Test-AppOwnedProcess -ProcessIdValue $ProcessIdValue)) {
    return $null
  }
  $processInfo = Get-ProcessInfo -ProcessIdValue $ProcessIdValue
  if ($null -eq $processInfo) {
    return $null
  }
  $fingerprint = Get-CommandLineFingerprint -CommandLine ([string]$processInfo.CommandLine)
  return [ordered]@{
    role = $Role
    pid = $ProcessIdValue
    port = $Port
    name = $processInfo.Name
    parent_pid = [int]$processInfo.ParentProcessId
    command_line_fingerprint = $fingerprint
    same_checkout = $true
  }
}

function Get-AppOwnedPortOwnerEvidence {
  param(
    [int]$Port,
    [string]$Role
  )

  # BLOCK 21: Collect evidence for app-owned processes listening on a required port.
  # WHY: Port recovery should use the actual listener PIDs, not only wrapper PIDs that may disappear when the terminal is closed with X.
  $records = @()
  foreach ($ownerId in @(Get-PortOwners -Port $Port)) {
    $record = Get-AppOwnedProcessEvidence -ProcessIdValue $ownerId -Role $Role -Port $Port
    if ($null -ne $record) {
      $records += $record
    }
  }
  return $records
}

function Stop-AppOwnedPortOwners {
  param(
    [int]$Port,
    [string]$Name,
    [string]$Reason
  )

  # BLOCK 22: Free a required port only when the listener is provably from this checkout.
  # WHY: This recovers from top-right-X launcher crashes while still refusing to kill unrelated Python, Node, Vite, or Uvicorn processes.
  $stoppedAny = $false
  foreach ($ownerId in @(Get-PortOwners -Port $Port)) {
    if (-not (Test-AppOwnedProcess -ProcessIdValue $ownerId)) {
      continue
    }
    Stop-AppOwnedProcessTree -RootProcessId $ownerId -Reason "$Reason on $Name port $Port"
    $stoppedAny = $true
  }
  if ($stoppedAny) {
    Start-Sleep -Seconds 1
  }
}

function Stop-StaleAppOwnedPortOwners {
  # BLOCK 23: Recover app-owned backend and frontend listeners that survived a prior launcher window close.
  # WHY: Closing a terminal with X can bypass normal shutdown, so the next startup must clean only same-checkout listeners before declaring the port blocked.
  Stop-AppOwnedPortOwners -Port $BACKEND_PORT -Name "Backend" -Reason "stale app-owned listener"
  Stop-AppOwnedPortOwners -Port $FRONTEND_PORT -Name "Frontend" -Reason "stale app-owned listener"
}

function Assert-PortAvailable {
  param(
    [int]$Port,
    [string]$Name
  )

  # BLOCK 24: Stop startup when a required port is already owned by another process.
  # WHY: VySol should either own its configured ports or fail with a readable message; it should not silently switch ports or kill unrelated apps.
  $owners = @(Get-PortOwners -Port $Port)
  if ($owners.Count -eq 0) {
    return
  }
  $details = foreach ($ownerId in $owners) {
    $process = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
      "PID $ownerId"
    }
    else {
      "PID $ownerId ($($process.ProcessName))"
    }
  }
  throw "$Name port $Port is already in use by $($details -join ', '). Stop that process or change the launcher config before running VySol."
}

function Invoke-Neo4jBootstrap {
  # BLOCK 25: Prepare Neo4j through the existing dedicated bootstrap script.
  # WHY: Neo4j setup has its own pinned downloads, credential handling, and local runtime folders, so the launcher should call it instead of duplicating that logic.
  $bootstrapPath = Join-Path $ROOT "scripts\bootstrap-neo4j.ps1"
  if (-not (Test-Path -LiteralPath $bootstrapPath)) {
    return
  }
  if (Test-Path -LiteralPath $NEO4J_PID_PATH) {
    Remove-Item -LiteralPath $NEO4J_PID_PATH -Force
  }
  Write-LauncherLog "Preparing local Neo4j..."
  try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $bootstrapPath -PidPath $NEO4J_PID_PATH
    if ($LASTEXITCODE -ne 0) {
      Write-LauncherLog "Local Neo4j could not be prepared. The app will still start; graph persistence will remain pending until Neo4j is available." "WARNING"
    }
    elseif (Test-Path -LiteralPath $NEO4J_PID_PATH) {
      $rawPid = (Get-Content -LiteralPath $NEO4J_PID_PATH -ErrorAction SilentlyContinue | Select-Object -First 1)
      if ($rawPid -match "^\d+$") {
        $script:StartedRootPids += [int]$rawPid
      }
    }
  }
  catch {
    Write-LauncherLog "Local Neo4j could not be prepared: $($_.Exception.Message)" "WARNING"
  }
}

function Start-LoggedCommand {
  param(
    [string]$Name,
    [string]$CommandLine,
    [string]$WorkingDirectory,
    [string]$OutputLog,
    [string]$ErrorLog
  )

  # BLOCK 26: Start one app-owned runtime process with stdout/stderr redirected into local logs.
  # WHY: Separate hidden runtime processes avoid extra terminal clutter while preserving enough logs for failed startup diagnosis.
  Write-LauncherLog "Starting $Name..."
  $process = Start-Process -FilePath "cmd.exe" `
    -ArgumentList @("/d", "/c", $CommandLine) `
    -WorkingDirectory $WorkingDirectory `
    -RedirectStandardOutput $OutputLog `
    -RedirectStandardError $ErrorLog `
    -WindowStyle Hidden `
    -PassThru
  if ($null -eq $process) {
    throw "Failed to start $Name."
  }
  $script:StartedRootPids += [int]$process.Id
  return $process
}

function Save-RunState {
  param(
    [int]$BackendPid,
    [int]$FrontendPid,
    [int]$Neo4jPid = 0
  )

  # BLOCK 27: Store the app-owned root PIDs plus any actual listener PIDs seen on required ports.
  # VARS: processRecords = safe process evidence without raw command lines, seenProcessIds = PID set used to avoid duplicate evidence, *_listener_pids = real port owners after readiness when available
  # WHY: Terminal-close crashes can orphan child listeners, so restart cleanup needs more than wrapper PIDs.
  $backendPortRecords = @(Get-AppOwnedPortOwnerEvidence -Port $BACKEND_PORT -Role "backend-listener")
  $frontendPortRecords = @(Get-AppOwnedPortOwnerEvidence -Port $FRONTEND_PORT -Role "frontend-listener")
  $processRecords = @()
  foreach ($record in @(
      (Get-AppOwnedProcessEvidence -ProcessIdValue $BackendPid -Role "backend-root"),
      (Get-AppOwnedProcessEvidence -ProcessIdValue $FrontendPid -Role "frontend-root")
    )) {
    if ($null -ne $record) {
      $processRecords += $record
    }
  }
  if ($Neo4jPid -gt 0) {
    $neo4jRecord = Get-AppOwnedProcessEvidence -ProcessIdValue $Neo4jPid -Role "neo4j-root"
    if ($null -ne $neo4jRecord) {
      $processRecords += $neo4jRecord
    }
  }
  $processRecords += $backendPortRecords
  $processRecords += $frontendPortRecords
  $uniqueProcessRecords = @()
  $seenProcessIds = @{}
  foreach ($record in $processRecords) {
    $recordPid = [string]$record.pid
    if ($seenProcessIds.ContainsKey($recordPid)) {
      continue
    }
    $seenProcessIds[$recordPid] = $true
    $uniqueProcessRecords += $record
  }

  $state = [ordered]@{
    run_id = $RUN_ID
    root = $ROOT
    started_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    backend_pid = $BackendPid
    frontend_pid = $FrontendPid
    neo4j_pid = $Neo4jPid
    backend_listener_pids = @($backendPortRecords | ForEach-Object { $_.pid })
    frontend_listener_pids = @($frontendPortRecords | ForEach-Object { $_.pid })
    processes = $uniqueProcessRecords
    backend_port = $BACKEND_PORT
    frontend_port = $FRONTEND_PORT
    backend_health_url = $BACKEND_HEALTH_URL
    frontend_url = $FRONTEND_URL
    launcher_log = $LAUNCHER_LOG
    backend_log = $BACKEND_LOG
    frontend_log = $FRONTEND_LOG
  }
  $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $STATE_PATH -Encoding UTF8
  $script:StartedState = [pscustomobject]$state
}

function Test-UrlReady {
  param([string]$Url)

  # BLOCK 28: Check one local readiness endpoint without sending user data.
  # WHY: The browser should open only after the backend and frontend are both responding locally.
  try {
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
    return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
  }
  catch {
    return $false
  }
}

function Assert-ProcessStillRunning {
  param(
    [System.Diagnostics.Process[]]$Processes,
    [string]$Stage
  )

  # BLOCK 29: Fail readiness waits early if a started process exits.
  # WHY: A dead backend or frontend should point the user at logs immediately instead of waiting for a timeout.
  foreach ($process in $Processes) {
    $process.Refresh()
    if ($process.HasExited) {
      throw "$Stage failed because PID $($process.Id) exited early. See $LOG_DIR for runtime logs."
    }
  }
}

function Read-NewLogText {
  param(
    [string]$Path,
    [hashtable]$Offsets
  )

  # BLOCK 30: Read only the bytes appended to one runtime log since the last check.
  # VARS: Offsets = last byte position displayed for each log file, stream = shared-read file handle for logs still being written
  # WHY: Launcher log monitoring runs continuously, so rereading whole files every tick would get slower as logs grow.
  if (-not (Test-Path -LiteralPath $Path)) {
    return ""
  }

  $fileLength = (Get-Item -LiteralPath $Path).Length
  $offset = 0
  if ($Offsets.ContainsKey($Path)) {
    $offset = [int64]$Offsets[$Path]
  }
  if ($offset -gt $fileLength) {
    $offset = 0
  }
  if ($offset -eq $fileLength) {
    return ""
  }

  $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
  try {
    $stream.Seek($offset, [System.IO.SeekOrigin]::Begin) | Out-Null
    $reader = [System.IO.StreamReader]::new($stream)
    try {
      $text = $reader.ReadToEnd()
      $Offsets[$Path] = $stream.Position
      return $text
    }
    finally {
      $reader.Dispose()
    }
  }
  finally {
    $stream.Dispose()
  }
}

function Show-InitialLogTail {
  param(
    [hashtable]$Offsets,
    [hashtable]$LogFiles,
    [int]$TailLines = 40
  )

  # BLOCK 31: Show a bounded startup tail for each log, then mark the file as caught up.
  # VARS: TailLines = maximum existing lines shown once after readiness succeeds
  # WHY: Users need recent startup context in the single terminal, but the continuous loop should only stream appended bytes afterward.
  foreach ($label in $LogFiles.Keys) {
    $path = $LogFiles[$label]
    if (-not (Test-Path -LiteralPath $path)) {
      continue
    }
    foreach ($line in (Get-Content -LiteralPath $path -Tail $TailLines -ErrorAction SilentlyContinue)) {
      Write-Host "[$label] $line"
    }
    $Offsets[$path] = (Get-Item -LiteralPath $path).Length
  }
}

function Show-AppendedLogLines {
  param(
    [hashtable]$Offsets,
    [hashtable]$LogFiles
  )

  # BLOCK 32: Print only newly appended log text inside the single launcher terminal.
  # WHY: The monitoring loop should stay cheap and responsive even when backend or frontend logs grow large.
  foreach ($label in $LogFiles.Keys) {
    $text = Read-NewLogText -Path $LogFiles[$label] -Offsets $Offsets
    if ([string]::IsNullOrEmpty($text)) {
      continue
    }
    foreach ($line in ($text -split "\r?\n")) {
      if (-not [string]::IsNullOrEmpty($line)) {
        Write-Host "[$label] $line"
      }
    }
  }
}

function Wait-ForShutdownSignal {
  param([System.Diagnostics.Process[]]$Processes)

  # BLOCK 33: Keep the one visible terminal alive while streaming logs until the user stops the app.
  # VARS: canPollKeyboard = whether this host supports non-blocking key checks
  # WHY: Startup ownership needs one obvious control point, but the monitoring loop must not block while services keep running.
  $offsets = @{}
  $logFiles = @{
    backend = $BACKEND_LOG
    "backend-error" = $BACKEND_ERROR_LOG
    frontend = $FRONTEND_LOG
    "frontend-error" = $FRONTEND_ERROR_LOG
  }
  $canPollKeyboard = $true
  Write-Host ""
  Write-Host "Runtime logs:"
  Show-InitialLogTail -Offsets $offsets -LogFiles $logFiles
  Write-Host ""
  Write-Host "Press Enter in this window to stop VySol."

  while ($true) {
    Show-AppendedLogLines -Offsets $offsets -LogFiles $logFiles
    foreach ($process in $Processes) {
      $process.Refresh()
      if ($process.HasExited) {
        Write-LauncherLog "A tracked runtime process exited. The launcher will stop the remaining app-owned processes." "WARNING"
        return
      }
    }
    if ($canPollKeyboard) {
      try {
        if ([Console]::KeyAvailable) {
          $key = [Console]::ReadKey($true)
          if ($key.Key -eq [ConsoleKey]::Enter) {
            return
          }
        }
      }
      catch {
        $canPollKeyboard = $false
        Write-LauncherLog "This terminal does not support non-blocking Enter detection. Close the launcher window or press Ctrl+C to stop VySol." "WARNING"
      }
    }
    Start-Sleep -Seconds 1
  }
}

function Wait-ForReadyUrl {
  param(
    [string]$Name,
    [string]$Url,
    [int]$TimeoutSeconds,
    [System.Diagnostics.Process[]]$Processes
  )

  # BLOCK 34: Wait for one service to respond before moving to the next startup step.
  # WHY: Opening the browser before Vite and FastAPI are ready creates false blank-page failures for users.
  Write-LauncherLog "Waiting for $Name readiness at $Url..."
  $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
  while ($stopwatch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    Assert-ProcessStillRunning -Processes $Processes -Stage $Name
    if (Test-UrlReady -Url $Url) {
      Write-LauncherLog "$Name is ready."
      return
    }
    Start-Sleep -Seconds 1
  }
  throw "$Name did not become ready within $TimeoutSeconds seconds. See $LOG_DIR for runtime logs."
}

function Start-App {
  # BLOCK 35: Start backend and frontend only after stale PID cleanup and port checks pass.
  # WHY: Runtime ownership should be explicit before opening a browser or accepting user traffic.
  Assert-PortAvailable -Port $BACKEND_PORT -Name "Backend"
  Assert-PortAvailable -Port $FRONTEND_PORT -Name "Frontend"

  $backendCommand = "`"$PYTHON_EXE`" -m uvicorn backend.api.main:app --host 127.0.0.1 --port $BACKEND_PORT"
  $backendProcess = Start-LoggedCommand -Name "VySol backend" -CommandLine $backendCommand -WorkingDirectory $ROOT -OutputLog $BACKEND_LOG -ErrorLog $BACKEND_ERROR_LOG

  $frontendCommand = "npm.cmd run dev -- --strictPort"
  $frontendProcess = Start-LoggedCommand -Name "VySol frontend" -CommandLine $frontendCommand -WorkingDirectory $FRONTEND_DIR -OutputLog $FRONTEND_LOG -ErrorLog $FRONTEND_ERROR_LOG

  $neo4jPid = 0
  if (Test-Path -LiteralPath $NEO4J_PID_PATH) {
    $rawPid = (Get-Content -LiteralPath $NEO4J_PID_PATH -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($rawPid -match "^\d+$") {
      $neo4jPid = [int]$rawPid
    }
  }
  Save-RunState -BackendPid $backendProcess.Id -FrontendPid $frontendProcess.Id -Neo4jPid $neo4jPid

  Wait-ForReadyUrl -Name "Backend" -Url $BACKEND_HEALTH_URL -TimeoutSeconds 60 -Processes @($backendProcess, $frontendProcess)
  Wait-ForReadyUrl -Name "Frontend" -Url $FRONTEND_URL -TimeoutSeconds 90 -Processes @($backendProcess, $frontendProcess)
  Save-RunState -BackendPid $backendProcess.Id -FrontendPid $frontendProcess.Id -Neo4jPid $neo4jPid

  Write-LauncherLog "VySol is ready. Opening $FRONTEND_URL"
  Start-Process $FRONTEND_URL | Out-Null
  Write-Host ""
  Write-Host "VySol is running."
  Write-Host "Frontend: $FRONTEND_URL"
  Write-Host "Backend:  $BACKEND_HEALTH_URL"
  Write-Host "Logs:     $LOG_DIR"
  Write-Host ""
  Wait-ForShutdownSignal -Processes @($backendProcess, $frontendProcess)
}

try {
  # BLOCK 36: Run setup in the same order a user needs it for first launch.
  # WHY: Dependency and local-service readiness must be known before port ownership and app startup checks can be trusted.
  Initialize-Python
  Initialize-Node
  Sync-PythonDependencies
  Sync-FrontendDependencies
  Stop-RecordedAppProcesses -StatePath $STATE_PATH -Reason "stale prior run"
  Stop-StaleAppOwnedPortOwners
  Invoke-Neo4jBootstrap

  if ($SkipStart -or $env:VYSOL_SKIP_START) {
    Write-LauncherLog "Runtime checks and local dependency setup completed. Skipping app launch."
    exit 0
  }

  Start-App
}
catch {
  Write-LauncherLog $_.Exception.Message "ERROR"
  if ($script:StartedState) {
    Stop-RecordedAppProcesses -StatePath $STATE_PATH -Reason "failed startup"
  }
  else {
    foreach ($ProcessIdValue in ($script:StartedRootPids | Select-Object -Unique)) {
      Stop-AppOwnedProcessTree -RootProcessId $ProcessIdValue -Reason "failed startup"
    }
  }
  Write-Host ""
  Write-Host "Startup failed. See launcher log:"
  Write-Host $LAUNCHER_LOG
  exit 1
}
finally {
  # BLOCK 37: Clean up app-owned runtime processes when the launcher is done.
  # WHY: The run window is now the ownership boundary, so closing or stopping it should not leave backend/frontend processes orphaned.
  if ($script:StartedState) {
    Stop-RecordedAppProcesses -StatePath $STATE_PATH -Reason "launcher shutdown"
  }
  else {
    foreach ($ProcessIdValue in ($script:StartedRootPids | Select-Object -Unique)) {
      Stop-AppOwnedProcessTree -RootProcessId $ProcessIdValue -Reason "launcher shutdown"
    }
  }
}
