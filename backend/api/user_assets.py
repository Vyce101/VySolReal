"""User asset library storage for uploaded UI images and fonts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from backend.ingestion.text_sources.storage import atomic_write_json
from backend.logger import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_ROOT = REPO_ROOT / "user"
ASSET_ROOT = USER_ROOT / "assets"
IMAGE_ROOT = ASSET_ROOT / "images"
FONT_ROOT = ASSET_ROOT / "fonts"
ASSET_METADATA_FILE = ASSET_ROOT / "assets.json"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_IMAGE_ASSET_ID = "default-image-world"
DEFAULT_FONT_ASSET_ID = "default-font-inter"
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif"}
ALLOWED_FONT_EXTENSIONS = {".ttf", ".otf"}
DEFAULT_IMAGE_ASSETS = (
    {
        "id": DEFAULT_IMAGE_ASSET_ID,
        "name": "Default World Image",
        "url": "/api/app-assets/default_world_image.png",
    },
    {
        "id": "default-image-dark-academy",
        "name": "Dark Academy",
        "url": "/api/app-assets/defaults/images/Dark%20Academy.png",
    },
    {
        "id": "default-image-desert-ruins",
        "name": "Desert Ruins",
        "url": "/api/app-assets/defaults/images/Desert%20Ruins.png",
    },
    {
        "id": "default-image-fantasy-valley",
        "name": "Fantasy Valley",
        "url": "/api/app-assets/defaults/images/Fantasy%20Valley.png",
    },
    {
        "id": "default-image-moonlit-shrine",
        "name": "Moonlit Shrine",
        "url": "/api/app-assets/defaults/images/Moonlit%20Shrine.png",
    },
    {
        "id": "default-image-neon-city",
        "name": "Neon City",
        "url": "/api/app-assets/defaults/images/Neon%20City.png",
    },
    {
        "id": "default-image-ruined-battlefield",
        "name": "Ruined Battlefield",
        "url": "/api/app-assets/defaults/images/Ruined%20Battlefield.png",
    },
    {
        "id": "default-image-sky-islands",
        "name": "Sky Islands",
        "url": "/api/app-assets/defaults/images/Sky%20Islands.png",
    },
    {
        "id": "default-image-snowfield-aurora",
        "name": "Snowfield Aurora",
        "url": "/api/app-assets/defaults/images/Snowfield%20Aurora.png",
    },
)
DEFAULT_FONT_ASSETS = (
    {
        "id": "default-font-almendra-bold",
        "name": "Almendra Bold",
        "url": "/api/app-assets/defaults/fonts/Almendra/Almendra-Bold.ttf",
    },
    {
        "id": "default-font-cinzel-bold",
        "name": "Cinzel Bold",
        "url": "/api/app-assets/defaults/fonts/Cinzel/Cinzel-Bold.ttf",
    },
    {
        "id": "default-font-im-fell-english-sc",
        "name": "IM FELL English SC",
        "url": "/api/app-assets/defaults/fonts/IM_Fell_English_SC/IMFellEnglishSC-Regular.ttf",
    },
    {
        "id": DEFAULT_FONT_ASSET_ID,
        "name": "Inter Regular",
        "url": "/api/app-assets/defaults/fonts/Inter/Inter-VariableFont_opsz,wght.ttf",
    },
    {
        "id": "default-font-orbitron-bold",
        "name": "Orbitron Bold",
        "url": "/api/app-assets/defaults/fonts/Orbitron/Orbitron-Bold.ttf",
    },
)


class AssetValidationError(ValueError):
    """Raised when a user upload or asset operation is not allowed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True, frozen=True)
class UserAsset:
    """Metadata for one reusable uploaded asset."""

    asset_id: str
    kind: str
    name: str
    original_filename: str
    stored_filename: str
    content_type: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "UserAsset":
        return cls(
            asset_id=str(payload["asset_id"]),
            kind=str(payload["kind"]),
            name=str(payload["name"]),
            original_filename=str(payload["original_filename"]),
            stored_filename=str(payload["stored_filename"]),
            content_type=str(payload["content_type"]),
            created_at=str(payload["created_at"]),
        )


def asset_catalog(*, asset_root: Path = ASSET_ROOT) -> dict[str, object]:
    """Return grouped user and default assets for the UI pickers."""
    # BLOCK 1: Build picker groups from metadata instead of scanning arbitrary user files
    # VARS: assets = trusted user asset records, not every loose file under user/assets
    # WHY: Metadata is the app-owned truth boundary, so orphaned or partially written files must not become selectable assets by accident
    assets = load_assets(asset_root=asset_root)
    user_images = [_asset_payload(asset, asset_root=asset_root) for asset in assets if asset.kind == "image"]
    user_fonts = [_asset_payload(asset, asset_root=asset_root) for asset in assets if asset.kind == "font"]
    return {
        "images": {
            "user": user_images,
            "default": [_default_image_payload(asset_id=str(asset["id"])) for asset in DEFAULT_IMAGE_ASSETS],
        },
        "fonts": {
            "user": user_fonts,
            "default": [_default_font_payload(asset_id=str(asset["id"])) for asset in DEFAULT_FONT_ASSETS],
        },
    }


def load_assets(*, asset_root: Path = ASSET_ROOT) -> list[UserAsset]:
    """Load asset metadata records."""
    metadata_path = asset_root / ASSET_METADATA_FILE.name
    if not metadata_path.exists():
        return []
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assets: list[UserAsset] = []
    for asset_payload in payload.get("assets", []):
        try:
            asset = UserAsset.from_dict(dict(asset_payload))
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping invalid asset metadata record.")
            continue
        if not _is_safe_asset_record(asset):
            logger.warning("Skipping unsafe asset metadata record: asset_id=%s", asset.asset_id)
            continue
        assets.append(asset)
    return assets


def upload_image_asset(
    *,
    content: bytes,
    original_filename: str,
    content_type: str,
    asset_root: Path = ASSET_ROOT,
) -> dict[str, object]:
    """Validate and persist an uploaded background image."""
    # BLOCK 1: Reject files that cannot be safely treated as browser-renderable background images
    # VARS: extension = lowercase uploaded suffix used only for allowlist validation and generated storage naming
    # WHY: User-supplied MIME types and names can be spoofed, so the backend validates both size and file signature before saving
    extension = _validated_extension(original_filename=original_filename, allowed_extensions=ALLOWED_IMAGE_EXTENSIONS)
    _validate_upload_size(content)
    _validate_image_signature(content=content, extension=extension)

    # BLOCK 2: Store the accepted image under a generated asset id and record only safe metadata
    # VARS: asset = durable library record returned to the frontend, not a local filesystem path
    # WHY: Generated ids avoid path traversal and let duplicate uploads coexist without exposing the user's original directory
    asset_id = str(uuid4())
    asset = UserAsset(
        asset_id=asset_id,
        kind="image",
        name=_safe_display_filename(original_filename) or "Uploaded Image",
        original_filename=Path(original_filename).name,
        stored_filename=f"{asset_id}{extension}",
        content_type=_image_content_type(extension),
        created_at=_utc_now(),
    )
    _stage_and_save_asset(content=content, asset=asset, asset_root=asset_root)
    logger.info("User image asset uploaded: asset_id=%s original_filename=%s", asset.asset_id, asset.original_filename)
    return _asset_payload(asset, asset_root=asset_root)


def upload_font_asset(
    *,
    content: bytes,
    original_filename: str,
    content_type: str,
    asset_root: Path = ASSET_ROOT,
) -> dict[str, object]:
    """Validate, parse, and persist an uploaded font."""
    # BLOCK 1: Read the font's real internal full name before it becomes selectable
    # VARS: font_name = full font name from the font name table, not a cleaned filename fallback
    # WHY: Mixed real font names and filename fallbacks would confuse users, so unreadable font names block the upload instead
    extension = _validated_extension(original_filename=original_filename, allowed_extensions=ALLOWED_FONT_EXTENSIONS)
    _validate_upload_size(content)
    font_name = _read_font_full_name(content)

    # BLOCK 2: Save the parsed font with generated storage and metadata
    # WHY: The dropdown can read durable metadata instead of parsing font files every time it opens
    asset_id = str(uuid4())
    asset = UserAsset(
        asset_id=asset_id,
        kind="font",
        name=font_name,
        original_filename=Path(original_filename).name,
        stored_filename=f"{asset_id}{extension}",
        content_type=_font_content_type(extension),
        created_at=_utc_now(),
    )
    _stage_and_save_asset(content=content, asset=asset, asset_root=asset_root)
    logger.info("User font asset uploaded: asset_id=%s font_name=%s", asset.asset_id, asset.name)
    return _asset_payload(asset, asset_root=asset_root)


def delete_asset(
    *,
    asset_id: str,
    worlds_root: Path,
    asset_root: Path = ASSET_ROOT,
) -> dict[str, object]:
    """Delete one uploaded asset and repair any world selections that used it."""
    # BLOCK 1: Remove only user-uploaded assets from the metadata library
    # WHY: Default assets are app-owned fallback contracts and must not be removable through a user asset endpoint
    if _is_default_asset_id(asset_id):
        raise AssetValidationError("DEFAULT_ASSET_DELETE_BLOCKED", "Default assets cannot be deleted.")
    assets = load_assets(asset_root=asset_root)
    asset = next((candidate for candidate in assets if candidate.asset_id == asset_id), None)
    if asset is None:
        raise AssetValidationError("ASSET_NOT_FOUND", "Uploaded asset not found.")

    # BLOCK 2: Repair selected world asset ids before removing the physical file
    # VARS: repaired_worlds = number of world UI metadata files changed to use the default asset
    # WHY: Worlds must never keep pointing at a deleted uploaded image or font after the delete operation completes
    repaired_worlds = repair_world_asset_references(asset_id=asset.asset_id, kind=asset.kind, worlds_root=worlds_root)
    remaining_assets = [candidate for candidate in assets if candidate.asset_id != asset.asset_id]
    _save_assets(remaining_assets, asset_root=asset_root)
    asset_path = _asset_path(asset=asset, asset_root=asset_root)
    if asset_path.exists():
        asset_path.unlink()
    logger.info("User asset deleted: asset_id=%s kind=%s repaired_worlds=%s", asset.asset_id, asset.kind, repaired_worlds)
    return {
        "deleted": True,
        "asset_id": asset.asset_id,
        "kind": asset.kind,
        "repaired_worlds": repaired_worlds,
    }


def delete_impact(*, asset_id: str, worlds_root: Path, asset_root: Path = ASSET_ROOT) -> dict[str, object]:
    """Return how many worlds would fall back if an uploaded asset is deleted."""
    # BLOCK 1: Count saved world selections that currently reference this asset
    # WHY: The two-click delete popup needs specific impact text before the user confirms destructive removal
    assets = load_assets(asset_root=asset_root)
    asset = next((candidate for candidate in assets if candidate.asset_id == asset_id), None)
    if asset is None or _is_default_asset_id(asset_id):
        raise AssetValidationError("ASSET_NOT_FOUND", "Uploaded asset not found.")
    affected_worlds = _count_world_asset_references(asset_id=asset_id, kind=asset.kind, worlds_root=worlds_root)
    return {
        "asset_id": asset.asset_id,
        "kind": asset.kind,
        "affected_worlds": affected_worlds,
    }


def repair_world_asset_references(*, asset_id: str, kind: str, worlds_root: Path) -> int:
    """Fallback saved world selections away from a deleted uploaded asset."""
    # BLOCK 1: Replace deleted asset ids in UI metadata with the matching default id
    # VARS: selection_key = metadata field that stores the selected image or font asset id
    # WHY: The backend has to repair all saved worlds, including ones that are not currently open in the frontend
    selection_key = "background_asset_id" if kind == "image" else "font_asset_id"
    default_asset_id = DEFAULT_IMAGE_ASSET_ID if kind == "image" else DEFAULT_FONT_ASSET_ID
    repaired_worlds = 0
    for ui_metadata_path in _ui_metadata_paths(worlds_root):
        ui_metadata = _read_json_file(ui_metadata_path)
        if ui_metadata.get(selection_key) != asset_id:
            continue
        ui_metadata[selection_key] = default_asset_id
        atomic_write_json(ui_metadata_path, ui_metadata)
        repaired_worlds += 1
    return repaired_worlds


def resolve_asset(asset_id: str, *, kind: str, asset_root: Path = ASSET_ROOT) -> dict[str, object] | None:
    """Return an asset payload when the id exists and matches the requested kind."""
    # BLOCK 1: Resolve default ids first, then user metadata records
    # WHY: Save validation needs one helper that treats app defaults and user uploads as the same picker contract
    if kind == "image" and _is_default_asset_id(asset_id):
        return _default_image_payload(asset_id=asset_id)
    if kind == "font" and _is_default_asset_id(asset_id):
        return _default_font_payload(asset_id=asset_id)
    for asset in load_assets(asset_root=asset_root):
        if asset.asset_id == asset_id and asset.kind == kind:
            return _asset_payload(asset, asset_root=asset_root)
    return None


def asset_file_path(asset_id: str, *, asset_root: Path = ASSET_ROOT) -> tuple[Path, str]:
    """Return the safe stored file path and content type for one uploaded asset."""
    # BLOCK 1: Look up the file from metadata and constrain it to the expected kind folder
    # WHY: The browser can request only by asset id, never by a user-controlled path segment
    for asset in load_assets(asset_root=asset_root):
        if asset.asset_id != asset_id:
            continue
        path = _asset_path(asset=asset, asset_root=asset_root)
        return path, asset.content_type
    raise AssetValidationError("ASSET_NOT_FOUND", "Uploaded asset not found.")


def _stage_and_save_asset(*, content: bytes, asset: UserAsset, asset_root: Path) -> None:
    # BLOCK 1: Write the uploaded bytes into a temporary file before committing metadata
    # VARS: temp_path = same-folder staging file, final_path = generated asset file path
    # WHY: Failed validation or metadata writes should not leave a half-created selectable asset in the library
    final_path = _asset_path(asset=asset, asset_root=asset_root)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=final_path.parent, suffix=".tmp") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, final_path)
        assets = load_assets(asset_root=asset_root)
        _save_assets([*assets, asset], asset_root=asset_root)
    except OSError as exc:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        if final_path.exists():
            final_path.unlink(missing_ok=True)
        raise AssetValidationError("ASSET_WRITE_FAILED", "The uploaded asset could not be saved.") from exc


def _save_assets(assets: list[UserAsset], *, asset_root: Path) -> None:
    # BLOCK 1: Persist only safe metadata fields for the asset library
    # WHY: The metadata file must be durable enough for app restarts without storing absolute local paths
    asset_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(asset_root / ASSET_METADATA_FILE.name, {"version": 1, "assets": [asset.to_dict() for asset in assets]})


def _asset_payload(asset: UserAsset, *, asset_root: Path) -> dict[str, object]:
    # BLOCK 1: Convert stored metadata into a browser-facing asset record
    # WHY: API callers need urls and ids, not local storage paths
    return {
        "id": asset.asset_id,
        "kind": asset.kind,
        "source": "user",
        "name": asset.name,
        "original_filename": asset.original_filename,
        "url": f"/api/user-assets/{asset.asset_id}",
        "deletable": True,
    }


def _default_image_payload(*, asset_id: str = DEFAULT_IMAGE_ASSET_ID) -> dict[str, object]:
    asset = _default_asset_by_id(DEFAULT_IMAGE_ASSETS, asset_id) or _default_asset_by_id(DEFAULT_IMAGE_ASSETS, DEFAULT_IMAGE_ASSET_ID)
    return {
        "id": asset["id"],
        "kind": "image",
        "source": "default",
        "name": asset["name"],
        "url": asset["url"],
        "deletable": False,
    }


def _default_font_payload(*, asset_id: str = DEFAULT_FONT_ASSET_ID) -> dict[str, object]:
    asset = _default_asset_by_id(DEFAULT_FONT_ASSETS, asset_id) or _default_asset_by_id(DEFAULT_FONT_ASSETS, DEFAULT_FONT_ASSET_ID)
    css_family = f'"vysol-default-font-{asset["id"]}"'
    return {
        "id": asset["id"],
        "kind": "font",
        "source": "default",
        "name": asset["name"],
        "url": asset["url"],
        "css_family": css_family,
        "deletable": False,
    }


def _default_asset_by_id(assets: tuple[dict[str, str], ...], asset_id: str) -> dict[str, str] | None:
    # BLOCK 1: Look up one bundled default asset by its stable id
    # WHY: Default assets are app-owned records, so deletes and resolution should use ids instead of filesystem scans
    return next((asset for asset in assets if asset["id"] == asset_id), None)


def _asset_path(*, asset: UserAsset, asset_root: Path) -> Path:
    # BLOCK 1: Place image and font files into separate generated-name folders
    # WHY: Keeping asset types split makes allowlist mistakes easier to spot and avoids mixing browser media with font files
    if not _is_safe_asset_record(asset):
        raise AssetValidationError("ASSET_METADATA_INVALID", "Uploaded asset metadata is invalid.")
    folder = image_root(asset_root) if asset.kind == "image" else font_root(asset_root)
    return folder / asset.stored_filename


def image_root(asset_root: Path = ASSET_ROOT) -> Path:
    return asset_root / IMAGE_ROOT.name


def font_root(asset_root: Path = ASSET_ROOT) -> Path:
    return asset_root / FONT_ROOT.name


def _validated_extension(*, original_filename: str, allowed_extensions: set[str]) -> str:
    # BLOCK 1: Accept only known suffixes from the uploaded filename
    # WHY: The app should reject unsupported formats before doing any parsing or persistence work
    extension = Path(original_filename).suffix.lower()
    if extension not in allowed_extensions:
        raise AssetValidationError("ASSET_TYPE_UNSUPPORTED", "This file type is not supported.")
    return extension


def _validate_upload_size(content: bytes) -> None:
    # BLOCK 1: Enforce the shared upload size cap on the backend
    # WHY: Frontend checks are helpful but cannot be trusted, so the backend owns the real limit
    if len(content) == 0:
        raise AssetValidationError("ASSET_EMPTY", "The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise AssetValidationError("ASSET_TOO_LARGE", "Uploaded assets must be 2 MB or smaller.")


def _validate_image_signature(*, content: bytes, extension: str) -> None:
    # BLOCK 1: Match the file bytes against the declared image extension
    # WHY: Extension checks alone would allow a renamed non-image file into the browser-facing asset library
    if extension == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    if extension in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"):
        return
    if extension == ".webp" and len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return
    if extension == ".avif" and len(content) >= 16 and content[4:8] == b"ftyp" and (b"avif" in content[8:32] or b"avis" in content[8:32]):
        return
    raise AssetValidationError("IMAGE_SIGNATURE_INVALID", "The uploaded image could not be validated.")


def _read_font_full_name(content: bytes) -> str:
    # BLOCK 1: Parse only the name table from normal OpenType/TrueType files
    # WHY: This feature needs display metadata, not font transformation or designspace processing
    try:
        from fontTools.ttLib import TTFont
        from fontTools.ttLib import TTLibError
    except ImportError as exc:
        raise AssetValidationError("FONTTOOLS_UNAVAILABLE", "Font parsing is unavailable.") from exc

    try:
        font = TTFont(BytesIO(content), lazy=True)
        try:
            font_name = _best_name_table_value(font["name"], name_id=4)
        finally:
            font.close()
    except (KeyError, TTLibError, OSError, UnicodeDecodeError) as exc:
        raise AssetValidationError("FONT_NAME_UNREADABLE", "Could not read the font name from this file.") from exc
    if not font_name or not font_name.strip():
        raise AssetValidationError("FONT_NAME_UNREADABLE", "Could not read the font name from this file.")
    return " ".join(font_name.split())


def _best_name_table_value(name_table: object, *, name_id: int) -> str | None:
    # BLOCK 1: Prefer Windows Unicode name records, then accept any decodable matching name record
    # WHY: `getBestFullName()` can collapse to family names for minimal fonts, but this feature specifically requires the real full font name
    records = [record for record in getattr(name_table, "names", []) if getattr(record, "nameID", None) == name_id]
    preferred_records = sorted(records, key=lambda record: 0 if getattr(record, "platformID", None) == 3 else 1)
    for record in preferred_records:
        try:
            value = record.toUnicode()
        except UnicodeDecodeError:
            continue
        if value and value.strip():
            return value
    return None


def _image_content_type(extension: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".avif": "image/avif",
    }[extension]


def _font_content_type(extension: str) -> str:
    return {
        ".ttf": "font/ttf",
        ".otf": "font/otf",
    }[extension]


def _safe_display_filename(filename: str) -> str:
    # BLOCK 1: Keep a readable image label from the uploaded filename without using it as storage
    # WHY: Image names are user-facing labels, while generated storage names provide the safety boundary
    stem = Path(filename).stem.strip()
    return re.sub(r"\s+", " ", stem)[:80]


def _ui_metadata_paths(worlds_root: Path) -> list[Path]:
    if not worlds_root.exists():
        return []
    return [
        world_dir / "ui_world.json"
        for world_dir in worlds_root.iterdir()
        if world_dir.is_dir() and world_dir.name != ".ui_assets" and (world_dir / "ui_world.json").exists()
    ]


def _count_world_asset_references(*, asset_id: str, kind: str, worlds_root: Path) -> int:
    selection_key = "background_asset_id" if kind == "image" else "font_asset_id"
    return sum(1 for path in _ui_metadata_paths(worlds_root) if _read_json_file(path).get(selection_key) == asset_id)


def _read_json_file(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _is_default_asset_id(asset_id: str) -> bool:
    default_asset_ids = {str(asset["id"]) for asset in (*DEFAULT_IMAGE_ASSETS, *DEFAULT_FONT_ASSETS)}
    return asset_id in default_asset_ids


def _is_safe_asset_record(asset: UserAsset) -> bool:
    # BLOCK 1: Reject metadata that tries to make a generated filename behave like a path
    # WHY: Local metadata can be edited, so serving by asset id must still stay inside the image/font asset folders
    if asset.kind not in {"image", "font"}:
        return False
    if Path(asset.stored_filename).name != asset.stored_filename:
        return False
    return bool(asset.asset_id and asset.stored_filename)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
