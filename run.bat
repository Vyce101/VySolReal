@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
pushd "%ROOT%" || exit /b 1

set "VENV=%ROOT%venv"
set "PYTHON_EXE=%VENV%\Scripts\python.exe"
set "ACTIVATE=%VENV%\Scripts\activate.bat"
set "REQ_STAMP=%VENV%\.requirements.stamp"
set "FIRST_SETUP=0"

if not exist "%PYTHON_EXE%" (
  echo Setting up virtual environment...
  py -3.14 -m venv venv
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

if exist main.py (
  python main.py %*
  set "EXIT_CODE=%errorlevel%"
  popd
  pause
  exit /b %EXIT_CODE%
)

if exist app.py (
  python app.py %*
  set "EXIT_CODE=%errorlevel%"
  popd
  pause
  exit /b %EXIT_CODE%
)

if exist src\main.py (
  python src\main.py %*
  set "EXIT_CODE=%errorlevel%"
  popd
  pause
  exit /b %EXIT_CODE%
)

echo Virtual environment is ready, but no runnable entrypoint was found yet.
echo Add `main.py`, `app.py`, or `src\main.py` and run this script again.
popd
echo.
pause
exit /b 0
