"""FastAPI entrypoint for the VySol local app shell."""

from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.api.user_assets import (
    AssetValidationError,
    asset_file_path,
    delete_asset,
    delete_impact,
    upload_font_asset,
    upload_image_asset,
)
from backend.api.worlds import APP_ASSETS, WORLD_ASSETS, WORLD_ROOT, get_world_detail, list_worlds, save_world_detail
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
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
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


@app.get("/api/worlds/{world_uuid}/detail")
def world_detail(world_uuid: str) -> dict[str, object]:
    """Return editable detail data for one world."""
    try:
        return get_world_detail(world_uuid)
    except ValueError as exc:
        raise _world_http_error(exc) from exc


@app.patch("/api/worlds/{world_uuid}/detail")
def update_world_detail(world_uuid: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
    """Save editable detail data for one world."""
    try:
        return save_world_detail(world_uuid, payload)
    except ValueError as exc:
        raise _world_http_error(exc) from exc


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


@app.post("/api/user-assets/images")
async def upload_user_image(request: Request) -> dict[str, object]:
    """Upload one reusable background image."""
    return await _upload_user_asset(request=request, kind="image")


@app.post("/api/user-assets/fonts")
async def upload_user_font(request: Request) -> dict[str, object]:
    """Upload one reusable font."""
    return await _upload_user_asset(request=request, kind="font")


@app.get("/api/user-assets/{asset_id}")
def user_asset(asset_id: str) -> FileResponse:
    """Serve one uploaded asset by generated id."""
    try:
        path, media_type = asset_file_path(asset_id)
    except AssetValidationError as exc:
        raise _asset_http_error(exc) from exc
    return _safe_file_response(
        root=path.parent,
        relative_path=path.name,
        missing_message="Uploaded asset not found.",
        media_type=media_type,
    )


@app.get("/api/user-assets/{asset_id}/delete-impact")
def user_asset_delete_impact(asset_id: str) -> dict[str, object]:
    """Return the saved-world impact of deleting one uploaded asset."""
    try:
        return delete_impact(asset_id=asset_id, worlds_root=WORLD_ROOT)
    except AssetValidationError as exc:
        raise _asset_http_error(exc) from exc


@app.delete("/api/user-assets/{asset_id}")
def delete_user_asset(asset_id: str) -> dict[str, object]:
    """Delete one uploaded asset and repair saved world selections."""
    try:
        return delete_asset(asset_id=asset_id, worlds_root=WORLD_ROOT)
    except AssetValidationError as exc:
        raise _asset_http_error(exc) from exc


def _safe_file_response(*, root: Path, relative_path: str, missing_message: str, media_type: str | None = None) -> FileResponse:
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
    return FileResponse(resolved_path, media_type=media_type)


async def _upload_user_asset(*, request: Request, kind: str) -> dict[str, object]:
    # BLOCK 1: Read raw upload bytes and metadata from headers instead of multipart form parsing
    # VARS: original_filename = browser-provided filename used for validation labels only
    # WHY: Raw-body uploads avoid adding a separate multipart dependency while still letting the backend own validation
    original_filename = request.headers.get("x-asset-filename", "")
    if not original_filename:
        raise HTTPException(status_code=400, detail={"code": "ASSET_FILENAME_REQUIRED", "message": "Uploaded assets need a filename."})
    content = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")
    try:
        if kind == "image":
            asset = upload_image_asset(content=content, original_filename=original_filename, content_type=content_type)
        else:
            asset = upload_font_asset(content=content, original_filename=original_filename, content_type=content_type)
    except AssetValidationError as exc:
        raise _asset_http_error(exc) from exc
    return {"asset": asset}


def _asset_http_error(error: AssetValidationError) -> HTTPException:
    # BLOCK 1: Convert asset validation failures into stable API errors
    # WHY: The frontend needs codes for inline messages without receiving local file paths or Python exception details
    status_code = 404 if error.code == "ASSET_NOT_FOUND" else 400
    return HTTPException(status_code=status_code, detail={"code": error.code, "message": error.message})


def _world_http_error(error: ValueError) -> HTTPException:
    # BLOCK 1: Map world-detail validation codes to frontend-friendly HTTP responses
    # WHY: Save validation should stay structured and avoid leaking storage implementation details
    code = str(error)
    messages = {
        "WORLD_NOT_FOUND": "World not found.",
        "WORLD_NAME_REQUIRED": "World name is required.",
        "WORLD_NAME_DUPLICATE": "A world with this name already exists.",
        "BACKGROUND_ASSET_NOT_FOUND": "Selected background image was not found.",
        "FONT_ASSET_NOT_FOUND": "Selected font was not found.",
    }
    status_code = 404 if code == "WORLD_NOT_FOUND" else 400
    return HTTPException(status_code=status_code, detail={"code": code, "message": messages.get(code, "World detail could not be saved.")})
