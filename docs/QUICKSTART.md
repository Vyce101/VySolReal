# Quickstart

## Prerequisites
- Windows
- [Python 3.14 or newer](https://www.python.org/downloads/windows/) installed and available through `py` or `python` (`3.14.x` is the current tested family)
- [Node.js 20.19 or newer](https://nodejs.org/en/download) installed (`npm` comes with Node.js, and `24.x` is the current tested family)
- [Git](https://git-scm.com/downloads) installed if you are cloning from GitHub

## Install (PowerShell)
Open PowerShell in the folder where you want VySol installed, then copy and paste these exact commands:

```powershell
git clone https://github.com/Vyce101/VySolReal.git VySol
cd VySol
```

## Run
Run `run.bat` from the project folder.

On first run, `run.bat`:

- creates the local `venv`
- installs the pinned Python dependencies from `requirements.txt`
- installs the pinned frontend dependencies from `frontend/package-lock.json`
- prepares the local Neo4j Community database without installing a Windows service

The downloaded Neo4j tools are stored under `user/tools`, and Neo4j data plus the generated local connection file are stored under `user/neo4j`.

`run.bat` does **not** install Python, Node.js, or Git itself. React also does not need a separate install because it comes from the frontend dependency install.

If your Python or Node.js version is newer than the currently tested family, `run.bat` will show a warning and continue instead of hard-blocking you.
