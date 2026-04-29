# Quickstart

## Requirements

- OS support: Windows.
- Python: Python 3.14.0 or newer. VySol is currently tested against Python 3.14.x.
- Node.js: Node.js 20.19.0 or newer. VySol is currently tested against Node.js 24.x.
- Git: required for the Git command install path.
- Local services: no manual service install is required. `run.bat` prepares the local app runtime, backend, frontend, local Neo4j files, and local vector storage.
- API keys: provider API keys are required for AI-backed ingestion, embeddings, graph extraction, and future chat. The app can open before keys are configured.
- Hardware: no local GPU is required for cloud-provider workflows. Large worlds need more disk space, setup time, provider quota, and processing time.

## GIT COMMANDS

Open PowerShell in the folder where VySol should be installed.

Copy and paste this setup command:

```powershell
git clone https://github.com/Vyce101/VySolReal.git VySol
cd VySol
```

Run VySol:

```powershell
.\run.bat
```

After setup, run `run.bat` from the project folder whenever you want to start VySol again.

On first run, `run.bat`:

- creates the local `venv`
- installs pinned Python dependencies from `requirements.txt`
- installs pinned frontend dependencies from `frontend/package-lock.json`
- prepares the local Neo4j Community database without installing a Windows service
- starts the backend and frontend
- waits for both to be ready
- opens the app in your browser
- writes runtime logs under `user/logs/runtime`

Closing the `run.bat` window or pressing Enter in it stops the app-owned runtime processes that launcher started.

## DOWNLOADING LATEST INSTALL

1. Download the latest main ZIP: [VySol ZIP](https://github.com/Vyce101/VySolReal/archive/refs/heads/main.zip).
2. Unzip the downloaded file.
3. Open the unzipped `VySolReal-main` folder.
4. Run `run.bat`.

The ZIP path uses the latest main branch. Released app builds may not include every documented feature yet.
