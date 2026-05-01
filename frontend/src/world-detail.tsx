import * as React from "react";
import { ArrowLeft, Check, ChevronDown, Database, Image, Palette, Save, Trash2, Type, Upload, UserRound, X } from "lucide-react";
import {
  ApiError,
  AssetCatalog,
  AssetDeleteImpact,
  HubWorld,
  UserAsset,
  UserAssetKind,
  WorldDetail,
  absoluteApiUrl,
  deleteUserAsset,
  fetchDeleteImpact,
  fetchWorldDetail,
  saveWorldDetail,
  uploadUserAsset
} from "./api";

type DetailTab = "customize" | "ingestion";

type DetailDraft = {
  displayName: string;
  description: string;
  backgroundAssetId: string;
  fontAssetId: string;
};

type ArmedDelete = {
  assetId: string;
  unlockAt: number;
  impact: AssetDeleteImpact | null;
};

type WorldDetailScreenProps = {
  world: HubWorld;
  onBack: () => void;
  onWorldsChanged: () => void;
  logoUrl: string;
};

export function WorldDetailScreen({ world, onBack, onWorldsChanged, logoUrl }: WorldDetailScreenProps) {
  const [activeTab, setActiveTab] = React.useState<DetailTab>("customize");
  const [detail, setDetail] = React.useState<WorldDetail | null>(null);
  const [draft, setDraft] = React.useState<DetailDraft | null>(null);
  const [backgroundPickerOpen, setBackgroundPickerOpen] = React.useState(false);
  const [fontPickerOpen, setFontPickerOpen] = React.useState(false);
  const [armedDelete, setArmedDelete] = React.useState<ArmedDelete | null>(null);
  const [savePulse, setSavePulse] = React.useState(false);
  const [nameInvalid, setNameInvalid] = React.useState(false);
  const [statusMessage, setStatusMessage] = React.useState("");
  const [errorMessage, setErrorMessage] = React.useState("");
  const imageInputRef = React.useRef<HTMLInputElement | null>(null);
  const fontInputRef = React.useRef<HTMLInputElement | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    fetchWorldDetail(world.world_uuid)
      .then((nextDetail) => {
        if (cancelled) {
          return;
        }
        setDetail(nextDetail);
        setDraft(detailToDraft(nextDetail));
        setStatusMessage("");
        setErrorMessage("");
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setErrorMessage(error instanceof Error ? error.message : "World detail could not be loaded.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [world.world_uuid]);

  const isDirty = Boolean(detail && draft && !draftMatchesDetail(draft, detail));
  const selectedBackground = findAsset(detail?.assets, "image", draft?.backgroundAssetId) ?? detail?.selected_background;
  const selectedFont = findAsset(detail?.assets, "font", draft?.fontAssetId) ?? detail?.selected_font;
  const backgroundUrl = absoluteApiUrl(selectedBackground?.url ?? world.background_url);
  const fontCss = React.useMemo(() => buildUserFontCss(detail?.assets), [detail?.assets]);

  React.useEffect(() => {
    // BLOCK 1: Protect browser refresh or window close only while edits are dirty
    // WHY: The in-app red pulse cannot run when the browser itself is about to unload the page
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!isDirty) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [isDirty]);

  React.useEffect(() => {
    // BLOCK 1: Let Escape close transient pickers before it warns about unsaved page edits
    // WHY: Escape should feel local to the open control first, then become the page-level leave guard
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      if (backgroundPickerOpen || fontPickerOpen || armedDelete) {
        setBackgroundPickerOpen(false);
        setFontPickerOpen(false);
        setArmedDelete(null);
        return;
      }
      if (isDirty) {
        event.preventDefault();
        pulseSaveButton();
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [armedDelete, backgroundPickerOpen, fontPickerOpen, isDirty]);

  React.useEffect(() => {
    // BLOCK 1: Reset armed delete controls when the user clicks outside delete zones
    // WHY: The destructive action should stay contextual and easy to cancel with normal page interaction
    const handlePointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (!event.target.closest("[data-delete-zone='true']")) {
        setArmedDelete(null);
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  const pulseSaveButton = () => {
    setSavePulse(false);
    window.requestAnimationFrame(() => {
      setSavePulse(true);
      window.setTimeout(() => setSavePulse(false), 520);
    });
  };

  const guardDirtyNavigation = (action: () => void) => {
    // BLOCK 1: Convert unsafe navigation attempts into the lightweight save warning
    // WHY: The first shell should warn without introducing a full modal or routing system
    if (isDirty) {
      pulseSaveButton();
      return;
    }
    action();
  };

  const handleBack = () => {
    guardDirtyNavigation(onBack);
  };

  const handleTabChange = (nextTab: DetailTab) => {
    if (nextTab === activeTab) {
      return;
    }
    guardDirtyNavigation(() => {
      setActiveTab(nextTab);
      setBackgroundPickerOpen(false);
      setFontPickerOpen(false);
      setArmedDelete(null);
    });
  };

  const updateDraft = (partialDraft: Partial<DetailDraft>) => {
    setDraft((currentDraft) => (currentDraft ? { ...currentDraft, ...partialDraft } : currentDraft));
    setNameInvalid(false);
    setErrorMessage("");
    setStatusMessage("");
  };

  const handleSave = async () => {
    if (!detail || !draft) {
      return;
    }
    if (!isDirty) {
      return;
    }
    if (!draft.displayName.trim()) {
      setNameInvalid(true);
      pulseSaveButton();
      return;
    }
    try {
      const savedDetail = await saveWorldDetail(detail.world_uuid, {
        display_name: draft.displayName,
        description: draft.description,
        background_asset_id: draft.backgroundAssetId,
        font_asset_id: draft.fontAssetId
      });
      setDetail(savedDetail);
      setDraft(detailToDraft(savedDetail));
      setNameInvalid(false);
      setStatusMessage("Changes saved.");
      setErrorMessage("");
      onWorldsChanged();
    } catch (error: unknown) {
      if (error instanceof ApiError && error.code === "WORLD_NAME_DUPLICATE") {
        setNameInvalid(true);
      }
      setErrorMessage(error instanceof Error ? error.message : "Changes could not be saved.");
      pulseSaveButton();
    }
  };

  const handleDiscard = () => {
    if (!detail) {
      return;
    }
    setDraft(detailToDraft(detail));
    setNameInvalid(false);
    setStatusMessage("");
    setErrorMessage("");
    setBackgroundPickerOpen(false);
    setFontPickerOpen(false);
    setArmedDelete(null);
  };

  const handleUpload = async (kind: UserAssetKind, file: File | undefined) => {
    if (!file || !detail) {
      return;
    }
    try {
      const uploadedAsset = await uploadUserAsset(kind, file);
      const nextDetail = await fetchWorldDetail(detail.world_uuid);
      setDetail(nextDetail);
      setDraft((currentDraft) => {
        const baseDraft = currentDraft ?? detailToDraft(nextDetail);
        return kind === "image"
          ? { ...baseDraft, backgroundAssetId: uploadedAsset.id }
          : { ...baseDraft, fontAssetId: uploadedAsset.id };
      });
      setErrorMessage("");
      setStatusMessage(kind === "image" ? "Image uploaded." : "Font uploaded.");
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "Upload failed.");
    } finally {
      if (kind === "image" && imageInputRef.current) {
        imageInputRef.current.value = "";
      }
      if (kind === "font" && fontInputRef.current) {
        fontInputRef.current.value = "";
      }
    }
  };

  const handleDeleteAsset = async (asset: UserAsset) => {
    if (!asset.deletable || !detail || !draft) {
      return;
    }
    const now = Date.now();
    if (armedDelete?.assetId !== asset.id) {
      setArmedDelete({ assetId: asset.id, unlockAt: now + 1000, impact: null });
      try {
        const impact = await fetchDeleteImpact(asset.id);
        setArmedDelete((current) => (current?.assetId === asset.id ? { ...current, impact } : current));
      } catch {
        setArmedDelete(null);
      }
      return;
    }
    if (now < armedDelete.unlockAt) {
      return;
    }
    await deleteUserAsset(asset.id);
    const nextDetail = await fetchWorldDetail(detail.world_uuid);
    setDetail(nextDetail);
    setDraft((currentDraft) => {
      const baseDraft = currentDraft ?? detailToDraft(nextDetail);
      return asset.kind === "image" && baseDraft.backgroundAssetId === asset.id
        ? { ...baseDraft, backgroundAssetId: nextDetail.selected_background.id }
        : asset.kind === "font" && baseDraft.fontAssetId === asset.id
          ? { ...baseDraft, fontAssetId: nextDetail.selected_font.id }
          : baseDraft;
    });
    setArmedDelete(null);
    setStatusMessage(asset.kind === "image" ? "Image deleted." : "Font deleted.");
    onWorldsChanged();
  };

  if (!detail || !draft) {
    return (
      <main className="detail detail--loading">
        <WorldBackground backgroundUrl={absoluteApiUrl(world.background_url)} />
        <DetailHeader logoUrl={logoUrl} activeTab={activeTab} onTabChange={handleTabChange} onBack={handleBack} />
      </main>
    );
  }

  return (
    <main className="detail">
      <style>{fontCss}</style>
      <WorldBackground backgroundUrl={backgroundUrl} />
      <DetailHeader logoUrl={logoUrl} activeTab={activeTab} onTabChange={handleTabChange} onBack={handleBack} />
      {activeTab === "customize" ? (
        <section className="detail__content" aria-label="Customize world">
          <div className="detail__hero">
            <h1 style={{ fontFamily: userFontFamily(selectedFont) }}>{draft.displayName || detail.display_name}</h1>
            <p>Shape this world&apos;s identity and style.</p>
          </div>

          <div className="detail__grid">
            <section className="detail-card detail-card--identity" aria-label="World Identity">
              <h2 className="detail-card__title">
                <UserRound size={25} aria-hidden="true" />
                <span>World Identity</span>
              </h2>
              <div className="field-group">
                <label className={`field-label ${nameInvalid ? "field-label--invalid" : ""}`} htmlFor="world-name">
                  Display Name
                </label>
                <input
                  id="world-name"
                  className={`text-field ${nameInvalid ? "text-field--invalid" : ""}`}
                  value={draft.displayName}
                  style={{ fontFamily: userFontFamily(selectedFont) }}
                  onChange={(event) => updateDraft({ displayName: event.target.value })}
                />
              </div>
              <div className="field-group">
                <label className="field-label" htmlFor="world-description">Description</label>
                <textarea
                  id="world-description"
                  className="text-area"
                  value={draft.description}
                  style={{ fontFamily: userFontFamily(selectedFont) }}
                  onChange={(event) => updateDraft({ description: event.target.value })}
                />
              </div>
            </section>

            <section className="detail-card detail-card--style" aria-label="Visual Style">
              <h2 className="detail-card__title">
                <Palette size={25} aria-hidden="true" />
                <span>Visual Style</span>
              </h2>
              <div className="style-panel">
                <div className="field-group">
                  <label className="field-label">Background</label>
                  <button
                    type="button"
                    className="asset-select asset-select--image"
                    onClick={() => {
                      setBackgroundPickerOpen((open) => !open);
                      setFontPickerOpen(false);
                      setArmedDelete(null);
                    }}
                  >
                    <span className="asset-select__thumb" style={{ backgroundImage: `url("${absoluteApiUrl(selectedBackground?.url)}")` }} />
                    <span>{selectedBackground ? imageAssetLabel(selectedBackground) : "Default World Image"}</span>
                    <ChevronDown size={20} aria-hidden="true" />
                  </button>
                  <div className={`picker-expansion picker-expansion--image ${backgroundPickerOpen ? "picker-expansion--open" : ""}`}>
                    <ImagePicker
                      assets={detail.assets}
                      selectedAssetId={draft.backgroundAssetId}
                      armedDelete={armedDelete}
                      onUpload={() => imageInputRef.current?.click()}
                      onSelect={(asset) => updateDraft({ backgroundAssetId: asset.id })}
                      onDelete={handleDeleteAsset}
                    />
                  </div>
                </div>

                <div className="field-group">
                  <label className="field-label">World Font</label>
                  <button
                    type="button"
                    className="asset-select asset-select--font"
                    onClick={() => {
                      setFontPickerOpen((open) => !open);
                      setBackgroundPickerOpen(false);
                      setArmedDelete(null);
                    }}
                  >
                    <Type size={20} aria-hidden="true" />
                    <span style={{ fontFamily: selectedFont?.css_family ?? userFontFamily(selectedFont) }}>{selectedFont?.name ?? "Inter"}</span>
                    <ChevronDown size={20} aria-hidden="true" />
                  </button>
                  <div className={`picker-expansion picker-expansion--font ${fontPickerOpen ? "picker-expansion--open" : ""}`}>
                    <FontPicker
                      assets={detail.assets}
                      selectedAssetId={draft.fontAssetId}
                      armedDelete={armedDelete}
                      onUpload={() => fontInputRef.current?.click()}
                      onSelect={(asset) => updateDraft({ fontAssetId: asset.id })}
                      onDelete={handleDeleteAsset}
                    />
                  </div>
                </div>
              </div>
            </section>
          </div>

          <div className="detail-actions">
            <button
              type="button"
              className={`save-action ${isDirty ? "save-action--dirty" : ""} ${savePulse ? "save-action--pulse" : ""}`}
              onClick={handleSave}
              disabled={!isDirty}
            >
              <Save size={20} aria-hidden="true" />
              <span>Save Changes</span>
            </button>
            <button type="button" className="discard-action" onClick={handleDiscard}>
              <X size={20} aria-hidden="true" />
              <span>Discard</span>
            </button>
            {statusMessage ? <span className="save-note">{statusMessage}</span> : null}
            {errorMessage ? <span className="error-note">{errorMessage}</span> : null}
          </div>
        </section>
      ) : (
        <section className="detail__content detail__content--empty" aria-label="Ingestion" />
      )}
      <input ref={imageInputRef} type="file" accept=".png,.jpg,.jpeg,.webp,.avif" hidden onChange={(event) => handleUpload("image", event.target.files?.[0])} />
      <input ref={fontInputRef} type="file" accept=".ttf,.otf" hidden onChange={(event) => handleUpload("font", event.target.files?.[0])} />
    </main>
  );
}

function DetailHeader({
  logoUrl,
  activeTab,
  onTabChange,
  onBack
}: {
  logoUrl: string;
  activeTab: DetailTab;
  onTabChange: (tab: DetailTab) => void;
  onBack: () => void;
}) {
  return (
    <header className="detail-topbar">
      <div className="detail-brand-stack">
        <div className="brand" aria-label="VySol">
          <img className="brand__mark" src={logoUrl} alt="" />
          <span className="brand__wordmark">VySol</span>
        </div>
        <button type="button" className="context-back" onClick={onBack}>
          <ArrowLeft size={16} aria-hidden="true" />
          Worlds
        </button>
      </div>
      <nav className="detail-tabs" aria-label="World detail">
        <button type="button" className={activeTab === "customize" ? "detail-tabs__item detail-tabs__item--active" : "detail-tabs__item"} onClick={() => onTabChange("customize")}>
          <Palette size={21} aria-hidden="true" />
          Customize
        </button>
        <button type="button" className={activeTab === "ingestion" ? "detail-tabs__item detail-tabs__item--active" : "detail-tabs__item"} onClick={() => onTabChange("ingestion")}>
          <Database size={21} aria-hidden="true" />
          Ingestion
        </button>
      </nav>
    </header>
  );
}

function ImagePicker({
  assets,
  selectedAssetId,
  armedDelete,
  onUpload,
  onSelect,
  onDelete
}: {
  assets: AssetCatalog;
  selectedAssetId: string;
  armedDelete: ArmedDelete | null;
  onUpload: () => void;
  onSelect: (asset: UserAsset) => void;
  onDelete: (asset: UserAsset) => void;
}) {
  return (
    <div className="image-picker">
      <h3>User Images</h3>
      <div className="image-picker__grid">
        <button type="button" className="upload-tile" onClick={onUpload}>
          <Upload size={22} aria-hidden="true" />
          <span>Upload Image</span>
        </button>
        {assets.images.user.map((asset) => (
          <AssetImageTile key={asset.id} asset={asset} selected={selectedAssetId === asset.id} armedDelete={armedDelete} onSelect={onSelect} onDelete={onDelete} />
        ))}
      </div>
      <h3>Default Images</h3>
      <div className="image-picker__grid">
        {assets.images.default.map((asset) => (
          <AssetImageTile key={asset.id} asset={asset} selected={selectedAssetId === asset.id} armedDelete={armedDelete} onSelect={onSelect} onDelete={onDelete} />
        ))}
      </div>
    </div>
  );
}

function AssetImageTile({
  asset,
  selected,
  armedDelete,
  onSelect,
  onDelete
}: {
  asset: UserAsset;
  selected: boolean;
  armedDelete: ArmedDelete | null;
  onSelect: (asset: UserAsset) => void;
  onDelete: (asset: UserAsset) => void;
}) {
  const isArmed = armedDelete?.assetId === asset.id;
  return (
    <div className="asset-tile-wrap" data-delete-zone={isArmed ? "true" : undefined}>
      <button type="button" className={`image-tile ${selected ? "image-tile--selected" : ""}`} onClick={() => onSelect(asset)}>
        <span style={{ backgroundImage: `url("${absoluteApiUrl(asset.url)}")` }} />
        {selected ? <Check size={17} aria-hidden="true" /> : null}
      </button>
      {asset.deletable ? <DeleteButton asset={asset} armedDelete={armedDelete} onDelete={onDelete} /> : null}
    </div>
  );
}

function FontPicker({
  assets,
  selectedAssetId,
  armedDelete,
  onUpload,
  onSelect,
  onDelete
}: {
  assets: AssetCatalog;
  selectedAssetId: string;
  armedDelete: ArmedDelete | null;
  onUpload: () => void;
  onSelect: (asset: UserAsset) => void;
  onDelete: (asset: UserAsset) => void;
}) {
  const userFonts = [...assets.fonts.user].sort(compareAssetsByName);
  const defaultFonts = [...assets.fonts.default].sort(compareAssetsByName);

  return (
    <div className="font-picker">
      <button type="button" className="font-option font-option--upload" onClick={onUpload}>
        <Upload size={18} aria-hidden="true" />
        Upload Font
      </button>
      <div className="font-picker__divider" />
      <h3>User Fonts</h3>
      {userFonts.length ? userFonts.map((asset) => (
        <FontOption key={asset.id} asset={asset} selected={selectedAssetId === asset.id} armedDelete={armedDelete} onSelect={onSelect} onDelete={onDelete} />
      )) : <p className="picker-empty">No uploaded fonts yet.</p>}
      <div className="font-picker__divider" />
      <h3>Default Fonts</h3>
      {defaultFonts.map((asset) => (
        <FontOption key={asset.id} asset={asset} selected={selectedAssetId === asset.id} armedDelete={armedDelete} onSelect={onSelect} onDelete={onDelete} />
      ))}
    </div>
  );
}

function compareAssetsByName(left: UserAsset, right: UserAsset): number {
  return left.name.localeCompare(right.name, undefined, { sensitivity: "base" });
}

function imageAssetLabel(asset: UserAsset): string {
  if (!asset.original_filename) {
    return asset.name;
  }
  return asset.original_filename.replace(/\.[^/.]+$/, "") || asset.name;
}

function FontOption({
  asset,
  selected,
  armedDelete,
  onSelect,
  onDelete
}: {
  asset: UserAsset;
  selected: boolean;
  armedDelete: ArmedDelete | null;
  onSelect: (asset: UserAsset) => void;
  onDelete: (asset: UserAsset) => void;
}) {
  const isArmed = armedDelete?.assetId === asset.id;
  return (
    <div className="font-option-wrap" data-delete-zone={isArmed ? "true" : undefined}>
      <button type="button" className={`font-option ${selected ? "font-option--selected" : ""}`} onClick={() => onSelect(asset)} style={{ fontFamily: asset.css_family ?? userFontFamily(asset) }}>
        <span>{asset.name}</span>
        {selected ? <Check size={17} aria-hidden="true" /> : null}
      </button>
      {asset.deletable ? <DeleteButton asset={asset} armedDelete={armedDelete} onDelete={onDelete} /> : null}
    </div>
  );
}

function DeleteButton({
  asset,
  armedDelete,
  onDelete
}: {
  asset: UserAsset;
  armedDelete: ArmedDelete | null;
  onDelete: (asset: UserAsset) => void;
}) {
  const isArmed = armedDelete?.assetId === asset.id;
  return (
    <button type="button" className={`asset-delete ${isArmed ? "asset-delete--armed" : ""}`} onClick={(event) => {
      event.stopPropagation();
      onDelete(asset);
    }}>
      <Trash2 size={15} aria-hidden="true" />
      {isArmed ? (
        <span className="delete-popover">
          Deletes this {asset.kind}. {armedDelete.impact ? `${armedDelete.impact.affected_worlds} saved world${armedDelete.impact.affected_worlds === 1 ? "" : "s"} will fall back.` : "Checking impact."}
        </span>
      ) : null}
    </button>
  );
}

function WorldBackground({ backgroundUrl }: { backgroundUrl: string }) {
  return (
    <>
      <div className="detail__background" style={{ backgroundImage: `url("${backgroundUrl}")` }} aria-hidden="true" />
      <div className="detail__shade" aria-hidden="true" />
    </>
  );
}

function detailToDraft(detail: WorldDetail): DetailDraft {
  return {
    displayName: detail.display_name,
    description: detail.description,
    backgroundAssetId: detail.selected_background.id,
    fontAssetId: detail.selected_font.id
  };
}

function draftMatchesDetail(draft: DetailDraft, detail: WorldDetail): boolean {
  return (
    draft.displayName === detail.display_name &&
    draft.description === detail.description &&
    draft.backgroundAssetId === detail.selected_background.id &&
    draft.fontAssetId === detail.selected_font.id
  );
}

function findAsset(catalog: AssetCatalog | undefined, kind: UserAssetKind, assetId: string | undefined): UserAsset | undefined {
  if (!catalog || !assetId) {
    return undefined;
  }
  const groups = kind === "image" ? catalog.images : catalog.fonts;
  return [...groups.user, ...groups.default].find((asset) => asset.id === assetId);
}

function buildUserFontCss(catalog: AssetCatalog | undefined): string {
  if (!catalog) {
    return "";
  }
  return [...catalog.fonts.user, ...catalog.fonts.default]
    .filter((asset) => asset.url)
    .map((asset) => `@font-face{font-family:${userFontFamily(asset)};src:url("${absoluteApiUrl(asset.url)}");font-display:swap;}`)
    .join("\n");
}

function userFontFamily(asset: UserAsset | undefined): string | undefined {
  if (!asset) {
    return undefined;
  }
  if (asset.css_family) {
    return asset.css_family;
  }
  return `"vysol-user-font-${asset.id}"`;
}
