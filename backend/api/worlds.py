"""World hub data loading for the local app shell."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.embeddings.storage import load_world_metadata
from backend.ingestion.text_sources.storage import default_worlds_root
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
    slug: str
    title: str
    description: str
    background_url: str
    card_url: str
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
            logger.warning("Skipping world folder that could not be loaded: world_dir=%s error=%s", world_dir, exc)

    # BLOCK 3: Sort worlds by the newest usable activity signal, then title
    # WHY: The Hub's first real world should feel like the user's latest world, while folder modified time gives empty UI-only test worlds a safe fallback until real usage timestamps exist
    return sorted(worlds, key=_world_sort_key)


def _load_world(world_dir: Path) -> HubWorld:
    # BLOCK 1: Load optional UI-only metadata without requiring ingestion metadata
    # VARS: ui_metadata = personal Hub display metadata kept separate from world.json ingestion locks
    # WHY: Mock UI worlds must not look like ingested worlds or mutate backend ingestion state just to appear in the carousel
    ui_metadata_path = world_dir / UI_METADATA_FILE
    ui_metadata: dict[str, object] = {}
    if ui_metadata_path.exists():
        ui_metadata = json.loads(ui_metadata_path.read_text(encoding="utf-8-sig"))

    # BLOCK 2: Fall back to real world metadata only for the display title when UI metadata is absent
    # WHY: Existing ingested worlds should still appear in the Hub later, while UI-only test metadata remains the safer source for presentation fields
    world_metadata = load_world_metadata(world_dir)
    title = str(ui_metadata.get("title") or (world_metadata.world_name if world_metadata is not None else world_dir.name))
    slug = _slug_for_world(world_dir.name)
    description = str(ui_metadata.get("description") or "A local VySol world.")
    background_url = _asset_url(slug=slug, asset_name=ui_metadata.get("background_asset"))
    card_url = _asset_url(slug=slug, asset_name=ui_metadata.get("card_asset")) or background_url
    last_used_at = str(ui_metadata.get("last_used_at")) if ui_metadata.get("last_used_at") is not None else None

    return HubWorld(
        id=world_dir.name,
        slug=slug,
        title=title,
        description=description,
        background_url=background_url or FALLBACK_BACKGROUND_URL,
        card_url=card_url or FALLBACK_BACKGROUND_URL,
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
