# World Detail And Asset Library

## What This Is

World Detail is the first editable per-world shell opened from the Worlds Hub. It owns the Customize view for a world's display name, manual description, selected background image, and selected title/description font.

The Asset Library is the storage and catalog layer behind that view. It separates app-bundled defaults from user-uploaded images and fonts, serves both through browser-safe URLs, and keeps user assets in ignored local user storage.

## Why It Exists

VySol worlds need visual identity without mixing user uploads into tracked app files or exposing local filesystem paths to the frontend. The Asset Library gives the UI stable asset ids, grouped catalogs, and safe fallback behavior when user assets are deleted.

## Who This Page Is For

This page is for developers and AI coding agents working on the Worlds Hub, World Detail, local user storage, bundled default assets, or the upload/delete flows for backgrounds and fonts.

## What This System Owns

- The World Detail Customize shell for world identity and visual style.
- User-uploaded background image and font metadata.
- Generated asset ids and generated storage filenames for uploaded assets.
- Grouped image and font catalogs for the frontend.
- Bundled default image and font records.
- Per-world selected background and selected font ids.
- Safe fallback from deleted uploaded assets to app defaults.

## What This System Does Not Own

- Ingestion source files, chunks, embeddings, or graph data.
- Model provider settings or provider key scheduling.
- Legal review of third-party default font licenses.
- Browser-wide or app-wide theme font changes.
- Creating final default asset art.

## Normal Flow

The Hub opens a World Detail page for a selected world UUID. The backend returns the editable world identity, selected visual assets, and grouped asset catalogs. The Customize tab lets the user edit the display name, manual description, selected background, and selected font.

When the user saves, the backend validates the display name and selected asset ids together, then writes the world identity and UI visual metadata. Uploaded images and fonts are copied into user storage under generated filenames. Bundled defaults stay in app assets and are never copied into user storage.

## User-Facing Behavior

The background picker shows uploaded images separately from default images. The first default image is the app's default world image. Uploaded images can be deleted through the two-click destructive pattern; default images cannot.

The font picker shows upload, user fonts, and default fonts separately. A selected world font only affects that world's title and description surfaces: the World Detail title, display-name text, description text, and the Hub hero title and description for that world. It must not restyle the whole app shell, nav, labels, buttons, or unrelated worlds.

## Saved State

Uploaded user assets live under ignored user storage. Asset metadata stores ids, names, original filenames, generated storage filenames, content type, and creation time. World UI metadata stores selected background and font asset ids.

Bundled defaults live under app assets. Default font folders must keep their license files beside the font files so the app does not separate a font from its license text.

## Internal Edge Cases

- Blank display names are rejected.
- Duplicate display names are blocked after trimming whitespace and comparing case-insensitively.
- Empty descriptions remain empty and do not receive fallback text.
- Unsupported upload extensions are rejected.
- Oversized uploads are rejected before persistence.
- Fonts with unreadable internal full names are rejected instead of falling back to cleaned filenames.
- Default assets are never deletable through the user asset delete endpoint.
- Duplicate uploaded files are allowed as separate asset records.

## Cross-System Edge Cases

- World UUIDs must remain stable so renaming a world does not break saved asset selections.
- Hub summaries must carry or hydrate per-world selected font data so hover previews use the correct world font.
- Deleting an uploaded asset repairs saved world selections that pointed at it.
- Asset APIs must not return raw local filesystem paths.
- Backend logs in this area should use asset ids, world UUIDs, and source filenames instead of full local paths.

## Invariants

- User-uploaded assets must stay in ignored user storage.
- Default assets must stay app-owned and non-deletable.
- Asset ids, not filenames or paths, are the durable UI contract.
- Default font folders must keep license files with the font files.
- The selected world font must be scoped to world title and description text only.
- The default world image must appear first in the default image picker.

## Implementation Landmarks

World detail API behavior lives in the backend API world-detail and user-asset modules. The frontend detail shell lives in the World Detail React module, with the Hub applying per-world background and font previews from the world summary/detail contracts. Bundled default assets live under app assets, while uploaded user assets live under ignored user asset storage.

## What AI/Coders Must Check Before Changing This System

- Check both the Hub and World Detail views when changing selected font behavior.
- Check that user uploads still persist outside tracked files.
- Check that default assets remain non-deletable.
- Check that the default image picker still lists the default world image first.
- Check that no API response or normal log exposes raw local filesystem paths.
