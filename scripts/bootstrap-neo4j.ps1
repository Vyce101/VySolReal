param(
  [switch]$DryRun,
  [string]$PidPath
)

$ErrorActionPreference = "Stop"

# BLOCK 1: Keep all third-party download pins and review notes in one obvious place.
# VARS: NEO4J_* = pinned Neo4j Community archive information, JAVA_* = pinned portable Java 21 runtime information
# WHY: These URLs may need legal and release review before public distribution behavior changes, so they should not be scattered through launcher logic.
$NEO4J_VERSION = "5.26.25"
$NEO4J_ARCHIVE_NAME = "neo4j-community-$NEO4J_VERSION-windows.zip"
$NEO4J_DOWNLOAD_URL = "https://dist.neo4j.org/$NEO4J_ARCHIVE_NAME"
$JAVA_VERSION = "21.0.10+7"
$JAVA_ARCHIVE_NAME = "OpenJDK21U-jre_x64_windows_hotspot_21.0.10_7.zip"
$JAVA_DOWNLOAD_URL = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.10%2B7/$JAVA_ARCHIVE_NAME"
$LEGAL_REVIEW_NOTE = "Review Neo4j Community and Eclipse Temurin license/commercial terms before distributing a release that relies on these downloads."

# BLOCK 2: Resolve local runtime paths under ignored user folders.
# VARS: ROOT = repository root, TOOLS_DIR = ignored downloaded tool folder, NEO4J_RUNTIME_DIR = ignored database/runtime folder
# WHY: Keeping downloaded archives, extracted tools, credentials, and database files under user/ prevents local machine state from entering the public repo.
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT = Split-Path -Parent $SCRIPT_DIR
$USER_DIR = Join-Path $ROOT "user"
$TOOLS_DIR = Join-Path $USER_DIR "tools"
$DOWNLOAD_DIR = Join-Path $TOOLS_DIR "downloads"
$NEO4J_RUNTIME_DIR = Join-Path $USER_DIR "neo4j"
$NEO4J_CONF_DIR = Join-Path $NEO4J_RUNTIME_DIR "conf"
$NEO4J_CONNECTION_PATH = Join-Path $NEO4J_RUNTIME_DIR "connection.json"
$NEO4J_HOME = Join-Path $TOOLS_DIR "neo4j-community-$NEO4J_VERSION"

# BLOCK 3: Print dry-run actions without creating files, downloading archives, or generating credentials.
# WHY: Bootstrap behavior needs to be testable from scripts and CI-like checks without mutating a developer machine or leaking a generated password.
if ($DryRun) {
  Write-Host "Neo4j bootstrap dry run"
  Write-Host "Would use Neo4j Community $NEO4J_VERSION from $NEO4J_DOWNLOAD_URL"
  Write-Host "Would use portable Java $JAVA_VERSION from $JAVA_DOWNLOAD_URL"
  Write-Host "Would store tools under user/tools"
  Write-Host "Would store Neo4j runtime data under user/neo4j"
  Write-Host "Would create user/neo4j/connection.json once with a generated password, without printing it"
  Write-Host "Would start Neo4j locally without installing a Windows service"
  Write-Host $LEGAL_REVIEW_NOTE
  exit 0
}

function Write-Step {
  param([string]$Message)

  # BLOCK 4: Show only safe operational milestones.
  # WHY: The bootstrap must be understandable in run.bat output, but credentials and exact secret-bearing commands must stay out of the terminal.
  Write-Host "[neo4j] $Message"
}

function Convert-ToNeo4jPath {
  param([string]$Path)

  # BLOCK 5: Convert Windows paths to a Neo4j config-friendly form.
  # WHY: Neo4j config accepts forward slashes, which avoids accidental escape-sequence behavior from backslashes in generated config files.
  return $Path.Replace("\", "/")
}

function Ensure-Directory {
  param([string]$Path)

  # BLOCK 6: Create one required local runtime directory if it is missing.
  # WHY: The bootstrap writes only under ignored user folders, and creating each known folder explicitly avoids broad or surprising filesystem changes.
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Path $Path | Out-Null
  }
}

function Write-Utf8NoBomFile {
  param(
    [string]$Path,
    [object]$Value
  )

  # BLOCK 7: Write generated runtime files as UTF-8 without a byte-order mark.
  # VARS: content = text form written to disk, encoding = UTF-8 encoder configured to omit the BOM
  # WHY: Neo4j treats a BOM at the start of neo4j.conf as part of the first setting name, which makes a valid config fail strict validation.
  $encoding = [System.Text.UTF8Encoding]::new($false)
  if ($Value -is [array]) {
    $content = [string]::Join([Environment]::NewLine, $Value)
  }
  else {
    $content = [string]$Value
  }
  [System.IO.File]::WriteAllText($Path, $content, $encoding)
}

function Download-Archive {
  param(
    [string]$Url,
    [string]$ArchivePath
  )

  # BLOCK 8: Reuse a pinned archive if it already exists locally.
  # WHY: First run may need the network, but later launches should not redownload large third-party files every time.
  if (Test-Path -LiteralPath $ArchivePath) {
    return
  }

  # BLOCK 9: Download through a temporary file, then move it into place.
  # VARS: tempPath = incomplete download target that is safe to delete if the request fails
  # WHY: A failed network request should not leave a corrupt archive at the final path and break future idempotent runs.
  $tempPath = "$ArchivePath.tmp"
  if (Test-Path -LiteralPath $tempPath) {
    Remove-Item -LiteralPath $tempPath -Force
  }
  Invoke-WebRequest -Uri $Url -OutFile $tempPath -UseBasicParsing
  Move-Item -LiteralPath $tempPath -Destination $ArchivePath -Force
}

function Expand-ArchiveIfNeeded {
  param(
    [string]$ArchivePath,
    [string]$ExpectedPath
  )

  # BLOCK 10: Skip extraction when the expected tool folder is already present.
  # WHY: The extracted tool directory is versioned, so presence of that exact folder means this pinned dependency is already available.
  if (Test-Path -LiteralPath $ExpectedPath) {
    return
  }

  # BLOCK 11: Expand the pinned archive into user/tools.
  # WHY: Neo4j and Java stay portable and local to the app folder instead of being installed globally or as admin-managed services.
  Expand-Archive -LiteralPath $ArchivePath -DestinationPath $TOOLS_DIR -Force
}

function Get-JavaHome {
  # BLOCK 12: Find the extracted portable Java runtime by its java.exe.
  # WHY: The archive root folder is owned by Adoptium naming, so detecting the executable is safer than hardcoding an internal folder name.
  $javaExe = Get-ChildItem -Path $TOOLS_DIR -Recurse -Filter "java.exe" |
    Where-Object { $_.FullName -like "*21.0.10*" -and $_.FullName -like "*\bin\java.exe" } |
    Select-Object -First 1

  # BLOCK 13: Stop early if Java extraction did not produce the expected executable.
  # WHY: Starting Neo4j with a missing or wrong Java runtime causes noisy downstream errors that are harder to diagnose.
  if ($null -eq $javaExe) {
    throw "Portable Java 21 was not found after extraction."
  }

  return Split-Path -Parent (Split-Path -Parent $javaExe.FullName)
}

function New-Neo4jPassword {
  # BLOCK 14: Generate a local random password without using special characters.
  # VARS: alphabet = allowed characters, bytes = random bytes mapped into password characters
  # WHY: A long alphanumeric password avoids shell escaping problems when passed to Neo4j's first-start password command.
  $alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    [byte[]]$bytes = New-Object byte[] 48
    $rng.GetBytes($bytes)
    $characters = foreach ($byte in $bytes) {
      $alphabet[[int]$byte % $alphabet.Length]
    }
    return -join $characters
  }
  finally {
    $rng.Dispose()
  }
}

function Read-Neo4jConnection {
  # BLOCK 15: Read the existing connection file if one already exists.
  # WHY: The generated password is a one-time local secret; regenerating it would desync the saved credentials from the database.
  if (-not (Test-Path -LiteralPath $NEO4J_CONNECTION_PATH)) {
    return $null
  }

  return Get-Content -Raw -LiteralPath $NEO4J_CONNECTION_PATH | ConvertFrom-Json
}

function New-Neo4jConnection {
  # BLOCK 16: Refuse to invent a new password for an existing database.
  # WHY: If database files already exist without connection.json, the safe move is to stop instead of silently creating credentials that cannot log in.
  $dataDir = Join-Path $NEO4J_RUNTIME_DIR "data"
  if (Test-Path -LiteralPath $dataDir) {
    throw "Neo4j data exists but user/neo4j/connection.json is missing. Restore the connection file or remove the local Neo4j runtime data after confirming it is safe."
  }

  # BLOCK 17: Create the local connection contract with a generated password.
  # VARS: connection = local credential object consumed by future app code
  # WHY: Public tracked files can expose URI and username defaults, but the actual password must live only in ignored user state.
  $connection = [ordered]@{
    uri = "bolt://127.0.0.1:7687"
    username = "neo4j"
    password = New-Neo4jPassword
    created_at = (Get-Date).ToUniversalTime().ToString("o")
  }
  Write-Utf8NoBomFile -Path $NEO4J_CONNECTION_PATH -Value ($connection | ConvertTo-Json)
  return [pscustomobject]$connection
}

function Write-Neo4jConfig {
  # BLOCK 18: Generate the local Neo4j config that points all mutable state at user/neo4j.
  # VARS: configLines = Neo4j settings written to the generated neo4j.conf
  # WHY: The extracted Neo4j distribution should remain a replaceable tool, while database files and logs stay in the ignored runtime folder.
  Ensure-Directory $NEO4J_CONF_DIR
  $configPath = Join-Path $NEO4J_CONF_DIR "neo4j.conf"

  # BLOCK 19: Copy Neo4j's non-secret default config support files into the runtime config folder.
  # VARS: sourceConfDir = config folder from the extracted Neo4j archive
  # WHY: Pointing NEO4J_CONF at user/neo4j keeps mutable config local, but Neo4j still expects companion logging config files from the distribution.
  $sourceConfDir = Join-Path $NEO4J_HOME "conf"
  if (Test-Path -LiteralPath $sourceConfDir) {
    Get-ChildItem -LiteralPath $sourceConfDir -File |
      Where-Object { $_.Name -ne "neo4j.conf" } |
      ForEach-Object {
        $destination = Join-Path $NEO4J_CONF_DIR $_.Name
        if (-not (Test-Path -LiteralPath $destination)) {
          Copy-Item -LiteralPath $_.FullName -Destination $destination
        }
      }
  }

  # BLOCK 20: Build the generated neo4j.conf with only local listeners and ignored runtime directories.
  # WHY: The local bootstrap should not expose Neo4j on the network or write database/log state into the extracted tool folder.
  $dataDir = Convert-ToNeo4jPath (Join-Path $NEO4J_RUNTIME_DIR "data")
  $logsDir = Convert-ToNeo4jPath (Join-Path $NEO4J_RUNTIME_DIR "logs")
  $runDir = Convert-ToNeo4jPath (Join-Path $NEO4J_RUNTIME_DIR "run")
  $pluginsDir = Convert-ToNeo4jPath (Join-Path $NEO4J_RUNTIME_DIR "plugins")
  $importDir = Convert-ToNeo4jPath (Join-Path $NEO4J_RUNTIME_DIR "import")
  $configLines = @(
    "server.default_listen_address=127.0.0.1",
    "server.bolt.enabled=true",
    "server.bolt.listen_address=127.0.0.1:7687",
    "server.http.enabled=true",
    "server.http.listen_address=127.0.0.1:7474",
    "server.https.enabled=false",
    "server.directories.data=$dataDir",
    "server.directories.logs=$logsDir",
    "server.directories.run=$runDir",
    "server.directories.plugins=$pluginsDir",
    "server.directories.import=$importDir",
    "dbms.security.auth_enabled=true"
  )
  Write-Utf8NoBomFile -Path $configPath -Value $configLines
}

function Invoke-WithNeo4jEnvironment {
  param(
    [string]$JavaHome,
    [scriptblock]$ScriptBlock
  )

  # BLOCK 21: Temporarily point Neo4j commands at the portable Java runtime and generated config.
  # VARS: old* = caller environment values restored after Neo4j command execution
  # WHY: The launcher must not require global Java, admin changes, or permanent machine-level environment variables.
  $oldJavaHome = $env:JAVA_HOME
  $oldNeo4jHome = $env:NEO4J_HOME
  $oldNeo4jConf = $env:NEO4J_CONF
  $oldPath = $env:PATH
  try {
    $env:JAVA_HOME = $JavaHome
    $env:NEO4J_HOME = $NEO4J_HOME
    $env:NEO4J_CONF = $NEO4J_CONF_DIR
    $env:PATH = "$JavaHome\bin;$oldPath"
    & $ScriptBlock
  }
  finally {
    $env:JAVA_HOME = $oldJavaHome
    $env:NEO4J_HOME = $oldNeo4jHome
    $env:NEO4J_CONF = $oldNeo4jConf
    $env:PATH = $oldPath
  }
}

function Test-TcpPortOpen {
  param(
    [string]$HostName,
    [int]$Port
  )

  # BLOCK 22: Check whether the local Bolt port is already accepting connections.
  # WHY: The bootstrap should be idempotent and should not try to start a second Neo4j process on an occupied local port.
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $async = $client.BeginConnect($HostName, $Port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne(750)) {
      return $false
    }
    $client.EndConnect($async)
    return $true
  }
  catch {
    return $false
  }
  finally {
    $client.Close()
  }
}

function Set-InitialPasswordIfNeeded {
  param(
    [object]$Connection,
    [bool]$ConnectionWasCreated,
    [string]$JavaHome
  )

  # BLOCK 23: Only set Neo4j's initial password during first local bootstrap.
  # WHY: Neo4j's first-password command is intended to run before the database starts; rerunning it after users exist can fail or corrupt the expected credential flow.
  if (-not $ConnectionWasCreated) {
    return
  }

  # BLOCK 24: Pass the generated password to Neo4j without echoing it.
  # WHY: Neo4j needs the initial password before first startup, but the secret must never appear in launcher output.
  $adminBat = Join-Path $NEO4J_HOME "bin\neo4j-admin.bat"
  Invoke-WithNeo4jEnvironment -JavaHome $JavaHome -ScriptBlock {
    & $adminBat "dbms" "set-initial-password" "--require-password-change=false" $Connection.password
    if ($LASTEXITCODE -ne 0) {
      throw "Failed to set the initial Neo4j password."
    }
  }
}

function Start-Neo4jIfNeeded {
  param([string]$JavaHome)

  # BLOCK 25: Reuse a running local Neo4j listener if one is already present.
  # WHY: run.bat may be launched multiple times during development, and the bootstrap should not spawn duplicate database processes.
  if (Test-TcpPortOpen -HostName "127.0.0.1" -Port 7687) {
    Write-Step "Neo4j is already listening on bolt://127.0.0.1:7687."
    return
  }

  # BLOCK 26: Start Neo4j as a local process, not a Windows service.
  # WHY: Public run.bat should work without administrator rights or a machine-level service install.
  $neo4jBat = Join-Path $NEO4J_HOME "bin\neo4j.bat"
  Write-Step "Starting local Neo4j without installing a Windows service..."
  Invoke-WithNeo4jEnvironment -JavaHome $JavaHome -ScriptBlock {
    # BLOCK 27: Launch Neo4j's console mode in the background so no Windows service is required.
    # VARS: process = background Neo4j console process inherited from the temporary Java and Neo4j environment
    # WHY: On Windows, `neo4j start` expects an installed service; console mode is the portable path that works from ignored user/tools.
    $process = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$neo4jBat`" console") -WorkingDirectory $NEO4J_HOME -WindowStyle Hidden -PassThru
    if ($null -eq $process) {
      throw "Failed to start Neo4j."
    }
    if (-not [string]::IsNullOrWhiteSpace($PidPath)) {
      $pidDir = Split-Path -Parent $PidPath
      if (-not (Test-Path -LiteralPath $pidDir)) {
        New-Item -ItemType Directory -Path $pidDir | Out-Null
      }
      Set-Content -LiteralPath $PidPath -Value $process.Id -Encoding UTF8
    }
  }

  # BLOCK 28: Wait briefly for the local Bolt listener to accept connections.
  # WHY: The backend graph stage needs Bolt, and returning before startup completes would turn the first ingestion attempt into a false unavailable state.
  for ($attempt = 1; $attempt -le 30; $attempt++) {
    if (Test-TcpPortOpen -HostName "127.0.0.1" -Port 7687) {
      Write-Step "Neo4j is listening on bolt://127.0.0.1:7687."
      return
    }
    Start-Sleep -Seconds 1
  }
  throw "Neo4j started but Bolt did not become available on 127.0.0.1:7687."
}

# BLOCK 29: Prepare ignored folders for downloads, extracted tools, config, and database state.
# WHY: Each local artifact has to live under user/ so GitHub-ready checks do not pick up machine-specific runtime files.
Ensure-Directory $USER_DIR
Ensure-Directory $TOOLS_DIR
Ensure-Directory $DOWNLOAD_DIR
Ensure-Directory $NEO4J_RUNTIME_DIR

# BLOCK 30: Download and unpack the pinned portable Java runtime.
# WHY: Neo4j needs Java 21, and bundling it under user/tools avoids global installs while keeping the public repo free of binaries.
$javaArchivePath = Join-Path $DOWNLOAD_DIR $JAVA_ARCHIVE_NAME
Write-Step "Preparing portable Java 21..."
Download-Archive -Url $JAVA_DOWNLOAD_URL -ArchivePath $javaArchivePath
Expand-ArchiveIfNeeded -ArchivePath $javaArchivePath -ExpectedPath (Join-Path $TOOLS_DIR "jdk-21.0.10+7-jre")
$javaHome = Get-JavaHome

# BLOCK 31: Download and unpack the pinned Neo4j Community archive.
# WHY: Neo4j stays local to this workspace and can be started by run.bat without service registration.
$neo4jArchivePath = Join-Path $DOWNLOAD_DIR $NEO4J_ARCHIVE_NAME
Write-Step "Preparing Neo4j Community $NEO4J_VERSION..."
Download-Archive -Url $NEO4J_DOWNLOAD_URL -ArchivePath $neo4jArchivePath
Expand-ArchiveIfNeeded -ArchivePath $neo4jArchivePath -ExpectedPath $NEO4J_HOME

# BLOCK 32: Create or reuse local Neo4j credentials and generated config.
# WHY: Credentials are generated once, config is safe to rewrite, and neither belongs in tracked files.
$connection = Read-Neo4jConnection
$connectionWasCreated = $false
if ($null -eq $connection) {
  $connection = New-Neo4jConnection
  $connectionWasCreated = $true
}
Write-Neo4jConfig

# BLOCK 33: Set the first password once and start the local database.
# WHY: The app launch path should leave Neo4j ready before backend code needs graph storage, while preserving the one-time password contract.
Set-InitialPasswordIfNeeded -Connection $connection -ConnectionWasCreated $connectionWasCreated -JavaHome $javaHome
Start-Neo4jIfNeeded -JavaHome $javaHome
Write-Step "Neo4j bootstrap complete. Connection details are in user/neo4j/connection.json."
