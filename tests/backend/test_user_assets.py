"""User asset library and world detail API tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from backend.api import user_assets
from backend.api import worlds as world_api


class UserAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.asset_root = self.temp_dir / "user" / "assets"
        self.worlds_root = self.temp_dir / "user" / "worlds"
        self.worlds_root.mkdir(parents=True, exist_ok=True)
        self._old_world_root = world_api.WORLD_ROOT
        self._old_world_assets = world_api.WORLD_ASSETS
        self._old_asset_root = world_api.ASSET_ROOT
        world_api.WORLD_ROOT = self.worlds_root
        world_api.WORLD_ASSETS = self.worlds_root / ".ui_assets"
        world_api.ASSET_ROOT = self.asset_root

    def tearDown(self) -> None:
        world_api.WORLD_ROOT = self._old_world_root
        world_api.WORLD_ASSETS = self._old_world_assets
        world_api.ASSET_ROOT = self._old_asset_root
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_world_detail_backfills_uuid_and_keeps_empty_description(self) -> None:
        self._write_ui_world("Alpha", {"title": "Alpha", "description": ""})

        worlds = world_api.list_worlds()
        detail = world_api.get_world_detail(worlds[0].world_uuid)

        saved_metadata = json.loads((self.worlds_root / "Alpha" / "ui_world.json").read_text(encoding="utf-8"))
        self.assertEqual(detail["description"], "")
        self.assertEqual(saved_metadata["world_uuid"], worlds[0].world_uuid)

    def test_world_detail_rejects_blank_and_duplicate_names(self) -> None:
        self._write_ui_world("Alpha", {"title": "Alpha", "world_uuid": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"})
        self._write_ui_world("Beta", {"title": "Beta", "world_uuid": "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"})

        with self.assertRaisesRegex(ValueError, "WORLD_NAME_REQUIRED"):
            world_api.save_world_detail(
                "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                {
                    "display_name": "   ",
                    "description": "",
                    "background_asset_id": user_assets.DEFAULT_IMAGE_ASSET_ID,
                    "font_asset_id": user_assets.DEFAULT_FONT_ASSET_ID,
                },
            )

        with self.assertRaisesRegex(ValueError, "WORLD_NAME_DUPLICATE"):
            world_api.save_world_detail(
                "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                {
                    "display_name": " beta ",
                    "description": "",
                    "background_asset_id": user_assets.DEFAULT_IMAGE_ASSET_ID,
                    "font_asset_id": user_assets.DEFAULT_FONT_ASSET_ID,
                },
            )

    def test_image_upload_validates_type_and_size(self) -> None:
        asset = user_assets.upload_image_asset(
            content=self._png_bytes(),
            original_filename="background.png",
            content_type="image/png",
            asset_root=self.asset_root,
        )

        self.assertEqual(asset["kind"], "image")
        self.assertTrue((self.asset_root / "images").exists())

        with self.assertRaises(user_assets.AssetValidationError):
            user_assets.upload_image_asset(
                content=b"not-an-image",
                original_filename="bad.png",
                content_type="image/png",
                asset_root=self.asset_root,
            )

        with self.assertRaises(user_assets.AssetValidationError):
            user_assets.upload_image_asset(
                content=b"\x89PNG\r\n\x1a\n" + (b"0" * (user_assets.MAX_UPLOAD_BYTES + 1)),
                original_filename="huge.png",
                content_type="image/png",
                asset_root=self.asset_root,
            )

    def test_font_upload_reads_real_full_name_and_rejects_bad_font(self) -> None:
        asset = user_assets.upload_font_asset(
            content=self._font_bytes("Readable Font Regular"),
            original_filename="whatever.ttf",
            content_type="font/ttf",
            asset_root=self.asset_root,
        )

        self.assertEqual(asset["name"], "Readable Font Regular")
        self.assertEqual(asset["kind"], "font")

        with self.assertRaisesRegex(user_assets.AssetValidationError, "Could not read the font name"):
            user_assets.upload_font_asset(
                content=b"not-a-font",
                original_filename="broken.ttf",
                content_type="font/ttf",
                asset_root=self.asset_root,
            )

    def test_delete_uploaded_asset_repairs_saved_world_selections(self) -> None:
        image = user_assets.upload_image_asset(
            content=self._png_bytes(),
            original_filename="background.png",
            content_type="image/png",
            asset_root=self.asset_root,
        )
        self._write_ui_world(
            "Alpha",
            {
                "title": "Alpha",
                "world_uuid": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                "background_asset_id": image["id"],
            },
        )

        impact = user_assets.delete_impact(asset_id=str(image["id"]), worlds_root=self.worlds_root, asset_root=self.asset_root)
        result = user_assets.delete_asset(asset_id=str(image["id"]), worlds_root=self.worlds_root, asset_root=self.asset_root)

        saved_metadata = json.loads((self.worlds_root / "Alpha" / "ui_world.json").read_text(encoding="utf-8"))
        self.assertEqual(impact["affected_worlds"], 1)
        self.assertEqual(result["repaired_worlds"], 1)
        self.assertEqual(saved_metadata["background_asset_id"], user_assets.DEFAULT_IMAGE_ASSET_ID)

    def test_world_card_uses_selected_background_asset(self) -> None:
        image = user_assets.upload_image_asset(
            content=self._png_bytes(),
            original_filename="background.png",
            content_type="image/png",
            asset_root=self.asset_root,
        )
        self._write_ui_world(
            "Alpha",
            {
                "title": "Alpha",
                "world_uuid": "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
                "background_asset_id": image["id"],
                "card_asset": "stale-card.png",
            },
        )

        world = world_api.list_worlds()[0]

        self.assertEqual(world.background_url, image["url"])
        self.assertEqual(world.card_url, image["url"])

    def test_default_assets_cannot_be_deleted(self) -> None:
        with self.assertRaisesRegex(user_assets.AssetValidationError, "Default assets cannot be deleted"):
            user_assets.delete_asset(
                asset_id=user_assets.DEFAULT_IMAGE_ASSET_ID,
                worlds_root=self.worlds_root,
                asset_root=self.asset_root,
            )
        with self.assertRaisesRegex(user_assets.AssetValidationError, "Default assets cannot be deleted"):
            user_assets.delete_asset(
                asset_id="default-font-cinzel-bold",
                worlds_root=self.worlds_root,
                asset_root=self.asset_root,
            )

    def test_default_catalog_includes_bundled_assets_with_world_image_first(self) -> None:
        catalog = user_assets.asset_catalog(asset_root=self.asset_root)

        self.assertEqual(catalog["images"]["default"][0]["id"], user_assets.DEFAULT_IMAGE_ASSET_ID)
        self.assertTrue(any(asset["id"] == "default-image-dark-academy" for asset in catalog["images"]["default"]))
        self.assertFalse(any(asset["name"] in {"Main", "Main 2"} for asset in catalog["images"]["default"]))
        self.assertTrue(any(asset["id"] == "default-font-cinzel-bold" and "url" in asset for asset in catalog["fonts"]["default"]))

    def _write_ui_world(self, name: str, payload: dict[str, object]) -> None:
        world_dir = self.worlds_root / name
        world_dir.mkdir(parents=True, exist_ok=True)
        world_dir.joinpath("ui_world.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _png_bytes(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"test"

    def _font_bytes(self, full_name: str) -> bytes:
        font_builder = FontBuilder(1000, isTTF=True)
        font_builder.setupGlyphOrder([".notdef"])
        font_builder.setupCharacterMap({})
        glyph_pen = TTGlyphPen(None)
        glyph_pen.moveTo((0, 0))
        glyph_pen.lineTo((500, 0))
        glyph_pen.lineTo((500, 500))
        glyph_pen.lineTo((0, 500))
        glyph_pen.closePath()
        font_builder.setupGlyf({".notdef": glyph_pen.glyph()})
        font_builder.setupHorizontalMetrics({".notdef": (500, 0)})
        font_builder.setupHorizontalHeader(ascent=800, descent=-200)
        font_builder.setupOS2()
        font_builder.setupNameTable(
            {
                "familyName": full_name.rsplit(" ", 1)[0],
                "styleName": full_name.rsplit(" ", 1)[-1],
                "fullName": full_name,
            }
        )
        font_builder.setupPost()
        output = BytesIO()
        font_builder.save(output)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
