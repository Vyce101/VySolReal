export const API_BASE_URL = "http://127.0.0.1:8000";

export type HubWorld = {
  id: string;
  world_uuid: string;
  slug: string;
  title: string;
  description: string;
  background_url: string;
  card_url: string;
  selected_font?: UserAsset;
  used_last?: string;
  last_used_at?: string;
  chronicles?: number;
  order: number;
};

export type UserAssetKind = "image" | "font";

export type UserAsset = {
  id: string;
  kind: UserAssetKind;
  source: "user" | "default";
  name: string;
  original_filename?: string;
  url?: string;
  css_family?: string;
  deletable: boolean;
};

export type AssetCatalog = {
  images: {
    user: UserAsset[];
    default: UserAsset[];
  };
  fonts: {
    user: UserAsset[];
    default: UserAsset[];
  };
};

export type WorldDetail = {
  world_uuid: string;
  display_name: string;
  description: string;
  selected_background: UserAsset;
  selected_font: UserAsset;
  assets: AssetCatalog;
};

export type WorldDetailSavePayload = {
  display_name: string;
  description: string;
  background_asset_id: string;
  font_asset_id: string;
};

export type AssetDeleteImpact = {
  asset_id: string;
  kind: UserAssetKind;
  affected_worlds: number;
};

export class ApiError extends Error {
  code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export async function fetchWorlds(): Promise<HubWorld[]> {
  const payload = await fetchJson<{ worlds: HubWorld[] }>("/api/worlds");
  const worlds = payload.worlds;
  if (worlds.every((world) => world.selected_font)) {
    return worlds;
  }

  // BLOCK 1: Fill missing Hub font records from each world's detail contract
  // WHY: The Hub needs per-world font data for previews, and this keeps the UI correct even if the summary payload is missing newer visual metadata
  return Promise.all(worlds.map(async (world) => {
    if (world.selected_font) {
      return world;
    }
    try {
      const detail = await fetchWorldDetail(world.world_uuid);
      return { ...world, selected_font: detail.selected_font };
    } catch {
      return world;
    }
  }));
}

export async function fetchWorldDetail(worldUuid: string): Promise<WorldDetail> {
  return fetchJson<WorldDetail>(`/api/worlds/${encodeURIComponent(worldUuid)}/detail`);
}

export async function saveWorldDetail(worldUuid: string, payload: WorldDetailSavePayload): Promise<WorldDetail> {
  return fetchJson<WorldDetail>(`/api/worlds/${encodeURIComponent(worldUuid)}/detail`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export async function uploadUserAsset(kind: UserAssetKind, file: File): Promise<UserAsset> {
  // BLOCK 1: Upload the file as raw bytes with the original filename in a header
  // WHY: The backend intentionally avoids multipart parsing so the feature does not need another upload dependency
  const endpoint = kind === "image" ? "/api/user-assets/images" : "/api/user-assets/fonts";
  const response = await fetch(`${API_BASE_URL}${endpoint}`, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Asset-Filename": file.name
    },
    body: await file.arrayBuffer()
  });
  const payload = await parseResponse<{ asset: UserAsset }>(response);
  return payload.asset;
}

export async function fetchDeleteImpact(assetId: string): Promise<AssetDeleteImpact> {
  return fetchJson<AssetDeleteImpact>(`/api/user-assets/${encodeURIComponent(assetId)}/delete-impact`);
}

export async function deleteUserAsset(assetId: string): Promise<void> {
  await fetchJson(`/api/user-assets/${encodeURIComponent(assetId)}`, { method: "DELETE" });
}

export function absoluteApiUrl(url: string | undefined): string {
  if (!url) {
    return "";
  }
  if (url.startsWith("http")) {
    return url;
  }
  return `${API_BASE_URL}${url}`;
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  return parseResponse<T>(response);
}

async function parseResponse<T>(response: Response): Promise<T> {
  // BLOCK 1: Convert backend error payloads into one typed frontend error
  // WHY: UI controls need stable codes for validation states without parsing raw HTTP text
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const code = typeof detail?.code === "string" ? detail.code : `HTTP_${response.status}`;
    const message = typeof detail?.message === "string" ? detail.message : "The request failed.";
    throw new ApiError(code, message);
  }
  return payload as T;
}
