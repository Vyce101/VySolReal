"""FastAPI entrypoint for the VySol local app shell."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.api.worlds import APP_ASSETS, WORLD_ASSETS, list_worlds
from backend.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="VySol", version="0.1.0")

# BLOCK 1: Allow the Vite development frontend to call the local backend API
# VARS: allow_origins = browser origins used by the local development frontend
# WHY: The backend and frontend run on different local ports, so browsers block API calls unless the backend explicitly trusts the frontend origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    """Return a simple local health check."""
    return {"status": "ok"}


@app.get("/api/worlds")
def worlds() -> dict[str, object]:
    """Return worlds available to the UI hub."""
    logger.info("World hub list requested.")
    return {"worlds": [world.to_dict() for world in list_worlds()]}


@app.get("/api/worlds/{world_slug}/assets/{asset_name:path}")
def world_asset(world_slug: str, asset_name: str) -> FileResponse:
    """Serve a single approved world UI asset."""
    return _safe_file_response(
        root=WORLD_ASSETS / world_slug,
        relative_path=asset_name,
        missing_message="World asset not found.",
    )


@app.get("/api/app-assets/{asset_name:path}")
def app_asset(asset_name: str) -> FileResponse:
    """Serve app-owned fallback assets."""
    return _safe_file_response(
        root=APP_ASSETS,
        relative_path=asset_name,
        missing_message="App asset not found.",
    )


def _safe_file_response(*, root: Path, relative_path: str, missing_message: str) -> FileResponse:
    # BLOCK 1: Resolve requested asset paths and reject anything outside the approved asset root
    # VARS: resolved_root = trusted folder boundary, resolved_path = requested file after filesystem normalization
    # WHY: UI assets live beside local user data, so path traversal must be blocked before a FileResponse is created
    resolved_root = root.resolve(strict=False)
    resolved_path = (root / relative_path).resolve(strict=False)
    if resolved_root != resolved_path and resolved_root not in resolved_path.parents:
        raise HTTPException(status_code=404, detail=missing_message)

    # BLOCK 2: Return only existing files, never directories or guessed fallback paths
    # WHY: The frontend should receive stable 404s for missing local assets instead of backend directory listings or accidental user-data exposure
    if not resolved_path.is_file():
        raise HTTPException(status_code=404, detail=missing_message)
    return FileResponse(resolved_path)
