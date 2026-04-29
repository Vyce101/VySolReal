"""World hub data loading for the local app shell."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.api.user_assets import (
    DEFAULT_FONT_ASSET_ID,
    DEFAULT_IMAGE_ASSET_ID,
    ASSET_ROOT,
    AssetValidationError,
    asset_catalog,
    resolve_asset,
    upload_image_asset,
)
from backend.embeddings.storage import load_world_metadata
from backend.embeddings.storage import save_world_metadata
from backend.embeddings.storage import world_metadata_file_path
from backend.ingestion.text_sources.storage import default_worlds_root
from backend.ingestion.text_sources.storage import atomic_write_json
from backend.logger import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORLD_ROOT = default_worlds_root()
WORLD_ASSETS = WORLD_ROOT / ".ui_assets"
APP_ASSETS = REPO_ROOT / "docs" / "assets"
UI_METADATA_FILE = "ui_world.json"
FALLBACK_BACKGROUND_URL = "/api/app-assets/default_world_image.png"


@dataclass(slots=True, frozen=True)
class HubWorld:
    """World summary shown by the Hub carousel."""

    id: str
    world_uuid: str
    slug: str
    title: str
    description: str
    background_url: str
    card_url: str
    selected_font: dict[str, object]
    used_last: str | None
    last_used_at: str | None
    chronicles: int | None
    order: int
    local_modified_at: float

    def to_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None and key != "local_modified_at"}


def list_worlds() -> list[HubWorld]:
    """Return all worlds the Hub can show."""
    # BLOCK 1: Create the local worlds and UI-asset folders when the Hub first asks for world data
    # WHY: The app shell should start cleanly for a new user while keeping generated personal world data in ignored local storage
    WORLD_ROOT.mkdir(parents=True, exist_ok=True)
    WORLD_ASSETS.mkdir(parents=True, exist_ok=True)

    # BLOCK 2: Convert each world folder into a compact Hub summary and keep broken folders out of the UI response
    # VARS: worlds = loaded Hub summaries that have enough data for the carousel
    # WHY: One malformed personal test folder should not stop the entire app shell from opening
    worlds: list[HubWorld] = []
    for world_dir in sorted(path for path in WORLD_ROOT.iterdir() if path.is_dir() and path.name != ".ui_assets"):
        try:
            worlds.append(_load_world(world_dir))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Skipping world folder that could not be loaded: world_folder=%s error=%s", world_dir.name, exc)

    # BLOCK 3: Sort worlds by the newest usable activity signal, then title
    # WHY: The Hub's first real world should feel like the user's latest world, while folder modified time gives empty UI-only test worlds a safe fallback until real usage timestamps exist
    return sorted(worlds, key=_world_sort_key)


def get_world_detail(world_uuid: str) -> dict[str, object]:
    """Return editable world detail data plus available visual assets."""
    # BLOCK 1: Resolve the world from its durable UUID and build the editor payload
    # VARS: world_record = world folder plus saved metadata used by both fake UI worlds and real ingested worlds
    # WHY: The frontend should never care whether a temporary UI world or an ingested world supplied the metadata
    world_record = _find_world_record(world_uuid)
    if world_record is None:
        raise ValueError("WORLD_NOT_FOUND")
    world_dir, ui_metadata, world_metadata = world_record
    title = _world_title(world_dir=world_dir, ui_metadata=ui_metadata, world_metadata=world_metadata)
    description = str(ui_metadata.get("description") or "")
    background_asset_id = _selected_asset_id(ui_metadata, "background_asset_id", DEFAULT_IMAGE_ASSET_ID)
    font_asset_id = _selected_asset_id(ui_metadata, "font_asset_id", DEFAULT_FONT_ASSET_ID)
    selected_background = resolve_asset(background_asset_id, kind="image", asset_root=ASSET_ROOT) or resolve_asset(DEFAULT_IMAGE_ASSET_ID, kind="image", asset_root=ASSET_ROOT)
    selected_font = resolve_asset(font_asset_id, kind="font", asset_root=ASSET_ROOT) or resolve_asset(DEFAULT_FONT_ASSET_ID, kind="font", asset_root=ASSET_ROOT)

    return {
        "world_uuid": _world_uuid(world_dir=world_dir, ui_metadata=ui_metadata, world_metadata=world_metadata),
        "display_name": title,
        "description": description,
        "selected_background": selected_background,
        "selected_font": selected_font,
        "assets": asset_catalog(asset_root=ASSET_ROOT),
    }


def save_world_detail(world_uuid: str, payload: dict[str, object]) -> dict[str, object]:
    """Persist editable world identity and visual style settings."""
    # BLOCK 1: Validate the complete edit payload before writing any world files
    # VARS: display_name = trimmed user-facing name, description = manual text that may intentionally be empty
    # WHY: Name validation and asset validation need to fail together so partial saves do not split identity from visual style
    world_record = _find_world_record(world_uuid)
    if world_record is None:
        raise ValueError("WORLD_NOT_FOUND")
    world_dir, ui_metadata, world_metadata = world_record
    display_name = _normalized_display_name(payload.get("display_name"))
    if not display_name:
        raise ValueError("WORLD_NAME_REQUIRED")
    if _display_name_exists(display_name=display_name, current_world_uuid=world_uuid):
        raise ValueError("WORLD_NAME_DUPLICATE")
    description = str(payload.get("description") or "")
    background_asset_id = str(payload.get("background_asset_id") or DEFAULT_IMAGE_ASSET_ID)
    font_asset_id = str(payload.get("font_asset_id") or DEFAULT_FONT_ASSET_ID)
    if resolve_asset(background_asset_id, kind="image", asset_root=ASSET_ROOT) is None:
        raise ValueError("BACKGROUND_ASSET_NOT_FOUND")
    if resolve_asset(font_asset_id, kind="font", asset_root=ASSET_ROOT) is None:
        raise ValueError("FONT_ASSET_NOT_FOUND")

    # BLOCK 2: Save canonical name metadata and UI-only visual metadata in their proper files
    # WHY: Ingested worlds keep identity in world.json, while the presentation library stays in ui_world.json without renaming folders
    if world_metadata is not None:
        world_metadata.world_name = display_name
        save_world_metadata(world_metadata_file_path(world_dir), world_metadata)
    else:
        ui_metadata["title"] = display_name
        ui_metadata["world_uuid"] = world_uuid
    ui_metadata["description"] = description
    ui_metadata["background_asset_id"] = background_asset_id
    ui_metadata["font_asset_id"] = font_asset_id
    _save_ui_metadata(world_dir, ui_metadata)
    logger.info("World detail saved: world_uuid=%s", world_uuid)
    return get_world_detail(world_uuid)


def _load_world(world_dir: Path) -> HubWorld:
    # BLOCK 1: Load optional UI-only metadata without requiring ingestion metadata
    # VARS: ui_metadata = personal Hub display metadata kept separate from world.json ingestion locks
    # WHY: Mock UI worlds must not look like ingested worlds or mutate backend ingestion state just to appear in the carousel
    ui_metadata_path = world_dir / UI_METADATA_FILE
    ui_metadata: dict[str, object] = {}
    if ui_metadata_path.exists():
        ui_metadata = json.loads(ui_metadata_path.read_text(encoding="utf-8-sig"))
    world_metadata = load_world_metadata(world_dir)
    world_uuid = _ensure_world_uuid(world_dir=world_dir, ui_metadata=ui_metadata, world_metadata=world_metadata)
    slug = _slug_for_world(world_dir.name)
    _ensure_legacy_background_asset_id(world_dir=world_dir, slug=slug, ui_metadata=ui_metadata)

    # BLOCK 2: Resolve display metadata without inventing fallback description text
    # WHY: World descriptions are manual-only, and Hub fallback copy must not become fake user-authored metadata
    title = _world_title(world_dir=world_dir, ui_metadata=ui_metadata, world_metadata=world_metadata)
    description = str(ui_metadata.get("description") or "")
    background_url = _selected_image_url(ui_metadata=ui_metadata, slug=slug)
    font_asset_id = _selected_asset_id(ui_metadata, "font_asset_id", DEFAULT_FONT_ASSET_ID)
    selected_font = resolve_asset(font_asset_id, kind="font", asset_root=ASSET_ROOT) or resolve_asset(DEFAULT_FONT_ASSET_ID, kind="font", asset_root=ASSET_ROOT)
    last_used_at = str(ui_metadata.get("last_used_at")) if ui_metadata.get("last_used_at") is not None else None

    return HubWorld(
        id=world_dir.name,
        world_uuid=world_uuid,
        slug=slug,
        title=title,
        description=description,
        background_url=background_url or FALLBACK_BACKGROUND_URL,
        card_url=background_url or FALLBACK_BACKGROUND_URL,
        selected_font=selected_font or {},
        used_last=str(ui_metadata.get("used_last")) if ui_metadata.get("used_last") is not None else None,
        last_used_at=last_used_at,
        chronicles=int(ui_metadata["chronicles"]) if ui_metadata.get("chronicles") is not None else None,
        order=int(ui_metadata.get("order", 1000)),
        local_modified_at=world_dir.stat().st_mtime,
    )


def _world_sort_key(world: HubWorld) -> tuple[float, str]:
    # BLOCK 1: Turn the optional API/UI last-used timestamp into the newest-first sorting number
    # VARS: parsed_last_used = machine-readable timestamp when future UI code starts saving it
    # WHY: `used_last` is display text for humans, so sorting must use a separate timestamp that future ingestion or chronicle usage can update without parsing labels
    parsed_last_used = _parse_timestamp(world.last_used_at)
    if parsed_last_used is not None:
        return (-parsed_last_used, world.title.lower())
    return (-world.local_modified_at, world.title.lower())


def _parse_timestamp(value: str | None) -> float | None:
    # BLOCK 1: Accept common ISO timestamps and reject anything ambiguous
    # WHY: A bad local UI timestamp should fall back to folder modified time instead of crashing the Hub or silently sorting wrong
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _find_world_record(world_uuid: str) -> tuple[Path, dict[str, object], object | None] | None:
    # BLOCK 1: Scan world folders and normalize missing UI UUIDs as worlds are discovered
    # WHY: Temporary UI worlds and real worlds need one durable lookup contract before the detail UI can save edits safely
    list_worlds()
    if not WORLD_ROOT.exists():
        return None
    for world_dir in sorted(path for path in WORLD_ROOT.iterdir() if path.is_dir() and path.name != ".ui_assets"):
        ui_metadata = _load_ui_metadata(world_dir)
        world_metadata = load_world_metadata(world_dir)
        if _world_uuid(world_dir=world_dir, ui_metadata=ui_metadata, world_metadata=world_metadata) == world_uuid:
            return world_dir, ui_metadata, world_metadata
    return None


def _world_uuid(*, world_dir: Path, ui_metadata: dict[str, object], world_metadata: object | None) -> str:
    # BLOCK 1: Prefer real ingestion metadata and use UI metadata only for temporary UI-only worlds
    # WHY: Real worlds already own durable identity in world.json, while fake worlds still need the same frontend contract during UI construction
    if world_metadata is not None and hasattr(world_metadata, "world_uuid"):
        return str(world_metadata.world_uuid)
    return str(ui_metadata.get("world_uuid") or "")


def _ensure_world_uuid(*, world_dir: Path, ui_metadata: dict[str, object], world_metadata: object | None) -> str:
    # BLOCK 1: Backfill UI-only world UUIDs without changing folder names
    # WHY: Current fake worlds should behave like real worlds in the detail UI even before ingestion creates world.json
    if world_metadata is not None and hasattr(world_metadata, "world_uuid"):
        return str(world_metadata.world_uuid)
    existing_uuid = ui_metadata.get("world_uuid")
    if isinstance(existing_uuid, str) and existing_uuid:
        return existing_uuid
    world_uuid = str(uuid4())
    ui_metadata["world_uuid"] = world_uuid
    _save_ui_metadata(world_dir, ui_metadata)
    return world_uuid


def _world_title(*, world_dir: Path, ui_metadata: dict[str, object], world_metadata: object | None) -> str:
    # BLOCK 1: Use the canonical ingestion name when it exists, otherwise use UI metadata for temporary worlds
    # WHY: Display renames must not create a second competing name source for real ingested worlds
    if world_metadata is not None and hasattr(world_metadata, "world_name"):
        return str(world_metadata.world_name)
    return str(ui_metadata.get("title") or world_dir.name)


def _selected_asset_id(ui_metadata: dict[str, object], key: str, default_asset_id: str) -> str:
    # BLOCK 1: Read saved visual selections with safe defaults
    # WHY: Older UI metadata predates asset ids, and the detail API still needs to return a valid selected asset
    value = ui_metadata.get(key)
    return str(value) if isinstance(value, str) and value else default_asset_id


def _selected_image_url(*, ui_metadata: dict[str, object], slug: str) -> str:
    # BLOCK 1: Prefer the new asset-id selection and keep old fake-world image paths as legacy fallback
    # WHY: Existing test worlds should keep their current art while new saves move to generated asset ids
    background_asset_id = _selected_asset_id(ui_metadata, "background_asset_id", "")
    if background_asset_id:
        selected_asset = resolve_asset(background_asset_id, kind="image", asset_root=ASSET_ROOT)
        if selected_asset is not None:
            return str(selected_asset["url"])
    return _asset_url(slug=slug, asset_name=ui_metadata.get("background_asset")) or FALLBACK_BACKGROUND_URL


def _ensure_legacy_background_asset_id(*, world_dir: Path, slug: str, ui_metadata: dict[str, object]) -> None:
    # BLOCK 1: Promote old per-world fake UI background files into the reusable user asset library once
    # VARS: legacy_asset_name = old ui_world.json filename before asset ids existed
    # WHY: The new detail UI needs selected backgrounds to be real asset ids while preserving the current fake-world artwork
    if isinstance(ui_metadata.get("background_asset_id"), str) and ui_metadata.get("background_asset_id"):
        return
    legacy_asset_name = ui_metadata.get("background_asset")
    if not isinstance(legacy_asset_name, str) or Path(legacy_asset_name).name != legacy_asset_name:
        return
    legacy_asset_path = WORLD_ASSETS / slug / legacy_asset_name
    if not legacy_asset_path.is_file():
        return
    try:
        asset = upload_image_asset(
            content=legacy_asset_path.read_bytes(),
            original_filename=legacy_asset_name,
            content_type="application/octet-stream",
            asset_root=ASSET_ROOT,
        )
    except (OSError, AssetValidationError):
        return
    ui_metadata["background_asset_id"] = asset["id"]
    _save_ui_metadata(world_dir, ui_metadata)


def _display_name_exists(*, display_name: str, current_world_uuid: str) -> bool:
    # BLOCK 1: Compare saved display names case-insensitively and whitespace-insensitively
    # WHY: Users should not be able to create duplicate-looking worlds by changing capitalization or adding stray spaces
    requested_key = _display_name_key(display_name)
    for world in list_worlds():
        if world.world_uuid == current_world_uuid:
            continue
        if _display_name_key(world.title) == requested_key:
            return True
    return False


def _display_name_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def _normalized_display_name(value: object) -> str:
    return " ".join(str(value or "").split())


def _load_ui_metadata(world_dir: Path) -> dict[str, object]:
    ui_metadata_path = world_dir / UI_METADATA_FILE
    if not ui_metadata_path.exists():
        return {}
    return json.loads(ui_metadata_path.read_text(encoding="utf-8-sig"))


def _save_ui_metadata(world_dir: Path, ui_metadata: dict[str, object]) -> None:
    atomic_write_json(world_dir / UI_METADATA_FILE, ui_metadata)


def _asset_url(*, slug: str, asset_name: object) -> str | None:
    # BLOCK 1: Accept only simple relative asset filenames from UI metadata
    # WHY: Metadata is user-local and editable, so it should never be able to point the browser at arbitrary filesystem paths
    if not isinstance(asset_name, str) or not asset_name:
        return None
    if Path(asset_name).name != asset_name:
        return None
    return f"/api/worlds/{slug}/assets/{asset_name}"


def _slug_for_world(world_name: str) -> str:
    # BLOCK 1: Convert a folder name into the stable URL segment used for world UI assets
    # WHY: Folder names may contain spaces, while asset URLs need predictable browser-safe path segments
    slug = re.sub(r"[^a-z0-9]+", "-", world_name.lower()).strip("-")
    return slug or "world"
