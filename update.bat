@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
pushd "%ROOT%" || exit /b 1

git diff --quiet --ignore-submodules --
if errorlevel 1 (
  echo Working tree has tracked changes. Please commit, stash, or discard them before updating.
  popd
  pause
  exit /b 1
)

git diff --cached --quiet --ignore-submodules --
if errorlevel 1 (
  echo Working tree has staged tracked changes. Please commit or unstage them before updating.
  popd
  pause
  exit /b 1
)

echo Fetching updates from GitHub...
git fetch --all --prune
if errorlevel 1 (
  echo Failed to fetch updates.
  popd
  pause
  exit /b 1
)

echo Applying the latest fast-forward update...
git pull --ff-only
if errorlevel 1 (
  echo Update failed. The branch may need manual attention.
  popd
  pause
  exit /b 1
)

if exist venv\Scripts\python.exe (
  if exist requirements.txt (
    echo Syncing Python dependencies...
    venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
      echo Dependency sync failed.
      popd
      pause
      exit /b 1
    )
  )
)

echo Update complete.
popd
echo.
pause
exit /b 0
