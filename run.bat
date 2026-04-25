@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
pushd "%ROOT%" || exit /b 1

set "MIN_PYTHON_VERSION=3.14.0"
set "TESTED_PYTHON_FAMILY=3.14"
set "MIN_NODE_VERSION=20.19.0"
set "TESTED_NODE_MAJOR=24"
set "VENV=%ROOT%venv"
set "PYTHON_EXE=%VENV%\Scripts\python.exe"
set "ACTIVATE=%VENV%\Scripts\activate.bat"
set "REQ_STAMP=%VENV%\.requirements.stamp"
set "FRONTEND_DIR=%ROOT%frontend"
set "FRONTEND_STAMP=%FRONTEND_DIR%\node_modules\.package-lock.stamp"
set "FIRST_SETUP=0"
set "PYTHON_LAUNCHER="
set "PYTHON_SELECTOR="
set "PYTHON_LABEL="
set "PYTHON_VERSION="
set "NODE_VERSION="

call :detect_python
if errorlevel 1 (
  popd
  pause
  exit /b 1
)

call :check_python_version
if errorlevel 1 (
  popd
  pause
  exit /b 1
)

call :detect_node
if errorlevel 1 (
  popd
  pause
  exit /b 1
)

call :check_node_version
if errorlevel 1 (
  popd
  pause
  exit /b 1
)

if not exist "%PYTHON_EXE%" (
  echo Setting up virtual environment with %PYTHON_LABEL%...
  call %PYTHON_LAUNCHER% %PYTHON_SELECTOR% -m venv venv
  if errorlevel 1 (
    echo Failed to create the virtual environment.
    popd
    pause
    exit /b 1
  )
  set "FIRST_SETUP=1"
)

call "%ACTIVATE%"
if errorlevel 1 (
  echo Failed to activate the virtual environment.
  popd
  pause
  exit /b 1
)

if "%FIRST_SETUP%"=="1" (
  python -m pip install --upgrade pip==25.3
  if errorlevel 1 (
    echo Failed to upgrade pip.
    popd
    pause
    exit /b 1
  )

  if exist requirements.txt (
    python -m pip install -r requirements.txt
    if errorlevel 1 (
      echo Failed to install dependencies.
      popd
      pause
      exit /b 1
    )
    copy /Y requirements.txt "%REQ_STAMP%" >nul
  )
)

if exist requirements.txt (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "if (!(Test-Path -LiteralPath '%REQ_STAMP%') -or (Get-Item -LiteralPath 'requirements.txt').LastWriteTimeUtc -gt (Get-Item -LiteralPath '%REQ_STAMP%').LastWriteTimeUtc) { exit 1 }"
  if errorlevel 1 (
    echo Installing pinned Python dependencies from requirements.txt...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
      echo Failed to install dependencies.
      popd
      pause
      exit /b 1
    )
    copy /Y requirements.txt "%REQ_STAMP%" >nul
  )
)

if exist "%ROOT%scripts\bootstrap-neo4j.ps1" (
  echo Preparing local Neo4j...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\bootstrap-neo4j.ps1"
  if errorlevel 1 (
    echo Local Neo4j could not be prepared. The app will still start; graph persistence will remain pending until Neo4j is available.
  )
)

if exist "%FRONTEND_DIR%\package.json" (
  if not exist "%FRONTEND_DIR%\node_modules" (
    echo Installing pinned frontend dependencies...
    pushd "%FRONTEND_DIR%" || exit /b 1
    call npm.cmd install
    if errorlevel 1 (
      echo Failed to install frontend dependencies.
      popd
      popd
      pause
      exit /b 1
    )
    popd
    if exist "%FRONTEND_DIR%\package-lock.json" copy /Y "%FRONTEND_DIR%\package-lock.json" "%FRONTEND_STAMP%" >nul
  )

  if exist "%FRONTEND_DIR%\package-lock.json" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "if (!(Test-Path -LiteralPath '%FRONTEND_STAMP%') -or (Get-Item -LiteralPath '%FRONTEND_DIR%\package-lock.json').LastWriteTimeUtc -gt (Get-Item -LiteralPath '%FRONTEND_STAMP%').LastWriteTimeUtc) { exit 1 }"
    if errorlevel 1 (
      echo Installing pinned frontend dependencies...
      pushd "%FRONTEND_DIR%" || exit /b 1
      call npm.cmd install
      if errorlevel 1 (
        echo Failed to install frontend dependencies.
        popd
        popd
        pause
        exit /b 1
      )
      popd
      copy /Y "%FRONTEND_DIR%\package-lock.json" "%FRONTEND_STAMP%" >nul
    )
  )
)

if defined VYSOL_SKIP_START (
  echo Runtime checks and local dependency setup completed. Skipping app launch because VYSOL_SKIP_START is set.
  popd
  exit /b 0
)

echo.
echo Starting VySol backend:  http://127.0.0.1:8000
echo Starting VySol frontend: http://127.0.0.1:5173
echo.

start "VySol Backend" /D "%ROOT%" cmd /k ""%PYTHON_EXE%" -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000"

if exist "%FRONTEND_DIR%\package.json" (
  start "VySol Frontend" /D "%FRONTEND_DIR%" cmd /k "npm.cmd run dev"
  popd
  echo VySol is starting. Open http://127.0.0.1:5173 in your browser.
  echo Close the backend and frontend windows to stop the app.
  echo.
  pause
  exit /b 0
)

echo Backend started, but no frontend package was found.
echo Open http://127.0.0.1:8000/api/health to verify the backend.
popd
pause
exit /b 0

:detect_python
where py >nul 2>nul
if errorlevel 1 goto :detect_python_fallback

for /f "tokens=2 delims= " %%i in ('py -3 --version 2^>nul') do set "PYTHON_VERSION=%%i"
if not defined PYTHON_VERSION goto :detect_python_fallback

set "PYTHON_LAUNCHER=py"
set "PYTHON_SELECTOR=-3"
set "PYTHON_LABEL=py -3"
exit /b 0

:detect_python_fallback
set "PYTHON_VERSION="
where python >nul 2>nul
if errorlevel 1 goto :detect_python_missing

for /f "tokens=2 delims= " %%i in ('python --version 2^>nul') do set "PYTHON_VERSION=%%i"
if not defined PYTHON_VERSION goto :detect_python_missing

set "PYTHON_LAUNCHER=python"
set "PYTHON_SELECTOR="
set "PYTHON_LABEL=python"
exit /b 0

:detect_python_missing
echo.
echo Python 3.14 or newer is required before run.bat can continue.
echo Install Python from https://www.python.org/downloads/windows/
echo After installing, open a new terminal and run run.bat again.
echo.
exit /b 1

:check_python_version
powershell -NoProfile -ExecutionPolicy Bypass -Command "$current = [version]'%PYTHON_VERSION%'; $minimum = [version]'%MIN_PYTHON_VERSION%'; if ($current -ge $minimum) { exit 0 } exit 1"
if errorlevel 1 (
  echo.
  echo Found Python %PYTHON_VERSION%, but VySol requires at least Python %MIN_PYTHON_VERSION%.
  echo Install a newer Python from https://www.python.org/downloads/windows/
  echo.
  exit /b 1
)

for /f "tokens=1,2 delims=." %%a in ("%PYTHON_VERSION%") do (
  set "PYTHON_MAJOR=%%a"
  set "PYTHON_MINOR=%%b"
)

if not "%PYTHON_MAJOR%.%PYTHON_MINOR%"=="%TESTED_PYTHON_FAMILY%" (
  echo.
  echo Warning: VySol is currently developed against Python %TESTED_PYTHON_FAMILY%.x, but found %PYTHON_VERSION%.
  echo Continuing anyway.
  echo.
)
exit /b 0

:detect_node
where node >nul 2>nul
if errorlevel 1 (
  echo.
  echo Node.js 20.19 or newer is required before run.bat can continue.
  echo Install Node.js from https://nodejs.org/en/download
  echo After installing, open a new terminal and run run.bat again.
  echo.
  exit /b 1
)

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo.
  echo Node.js was found, but npm was not available on PATH.
  echo Reinstall Node.js from https://nodejs.org/en/download and then run run.bat again.
  echo.
  exit /b 1
)

for /f "usebackq delims=" %%i in (`node -p ^"process.versions.node^" 2^>nul`) do set "NODE_VERSION=%%i"
if not defined NODE_VERSION (
  echo.
  echo Node.js was found, but its version could not be read.
  echo Reinstall Node.js from https://nodejs.org/en/download and then run run.bat again.
  echo.
  exit /b 1
)

exit /b 0

:check_node_version
powershell -NoProfile -ExecutionPolicy Bypass -Command "$current = [version]'%NODE_VERSION%'; $minimum = [version]'%MIN_NODE_VERSION%'; if ($current -ge $minimum) { exit 0 } exit 1"
if errorlevel 1 (
  echo.
  echo Found Node.js %NODE_VERSION%, but VySol requires at least Node.js %MIN_NODE_VERSION%.
  echo Install a newer Node.js version from https://nodejs.org/en/download
  echo.
  exit /b 1
)

for /f "tokens=1 delims=." %%a in ("%NODE_VERSION%") do set "NODE_MAJOR=%%a"
if not "%NODE_MAJOR%"=="%TESTED_NODE_MAJOR%" (
  echo.
  echo Warning: VySol is currently developed against Node.js %TESTED_NODE_MAJOR%.x, but found %NODE_VERSION%.
  echo Continuing anyway.
  echo.
)
exit /b 0
