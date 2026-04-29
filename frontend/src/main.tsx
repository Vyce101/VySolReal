import * as React from "react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BookOpen, CircleHelp, Compass, Search, Settings, Sparkles } from "lucide-react";
import "@fontsource-variable/inter/wght.css";
import { API_BASE_URL, HubWorld, UserAsset, absoluteApiUrl, fetchWorlds } from "./api";
import { WorldDetailScreen } from "./world-detail";
import "./styles.css";

type HeroState = {
  title: string;
  description: string;
  backgroundUrl: string;
  mode: "world" | "create" | "empty";
  worldUuid: string | null;
  fontFamily?: string;
};

type BackgroundStack = {
  previousUrl: string | null;
  currentUrl: string;
  version: number;
  animate: boolean;
};

type ApplyHeroOptions = {
  instantBackground?: boolean;
};

const appAssets = {
  logo: `${API_BASE_URL}/api/app-assets/Butterfly_logo_compressed_centered.png`,
  defaultWorld: `${API_BASE_URL}/api/app-assets/default_world_image.png`,
  createCard: `${API_BASE_URL}/api/app-assets/create_new_world_button.png`
};
const fallbackHero: HeroState = {
  title: "VySol",
  description: "Create a world, bring in canon material, and build grounded roleplay context.",
  backgroundUrl: appAssets.defaultWorld,
  mode: "empty",
  worldUuid: null
};

function App() {
  const [worlds, setWorlds, reloadWorlds] = useWorlds();
  const [detailWorld, setDetailWorld] = React.useState<HubWorld | null>(null);
  const [hero, setHero] = React.useState<HeroState>(fallbackHero);
  const [backgroundStack, setBackgroundStack] = React.useState<BackgroundStack>({
    previousUrl: null,
    currentUrl: fallbackHero.backgroundUrl,
    version: 0,
    animate: false
  });
  const hubFontCss = React.useMemo(() => buildHubFontCss(worlds), [worlds]);

  // BLOCK 1: Apply the Hub hero text and choose whether its background crossfades or updates immediately
  // VARS: useInstantBackground = whether to skip the old background layer during this hero change
  // WHY: Hover previews should animate, but returning from World Detail must not reveal stale card or world backgrounds first
  const applyHero = React.useCallback((nextHero: HeroState, options?: ApplyHeroOptions) => {
    const useInstantBackground = options?.instantBackground === true;
    setHero(nextHero);
    setBackgroundStack((currentStack) => {
      if (currentStack.currentUrl === nextHero.backgroundUrl) {
        return useInstantBackground
          ? {
              ...currentStack,
              previousUrl: null,
              animate: false
            }
          : currentStack;
      }
      if (useInstantBackground) {
        return {
          previousUrl: null,
          currentUrl: nextHero.backgroundUrl,
          version: currentStack.version + 1,
          animate: false
        };
      }
      return {
        previousUrl: currentStack.currentUrl,
        currentUrl: nextHero.backgroundUrl,
        version: currentStack.version + 1,
        animate: true
      };
    });
  }, []);

  // BLOCK 1: Pick the first real world as the initial Hub focus after worlds load, but only while the Hub is visible
  // VARS: firstWorld = first backend world after backend-side ordering
  // WHY: Detail saves also refresh the world list; changing the hidden Hub during detail navigation can leave stale background images in the return transition stack
  React.useEffect(() => {
    if (detailWorld) {
      return;
    }
    const firstWorld = worlds[0];
    if (!firstWorld) {
      applyHero(fallbackHero);
      return;
    }
    applyHero(worldToHero(firstWorld));
  }, [applyHero, detailWorld, worlds]);

  // BLOCK 2: Return from World Detail with that same world focused in the Hub
  // WHY: The Back action should land on the world the user was editing, and replacing the background instantly prevents a wrong-image flash
  const closeWorldDetail = React.useCallback(() => {
    if (!detailWorld) {
      return;
    }
    applyHero(worldToHero(detailWorld), { instantBackground: true });
    setDetailWorld(null);
  }, [applyHero, detailWorld]);

  const showCreateHero = () => {
    applyHero({
      title: "Create New World",
      description: "Start a new world space for sources, lore, and future chronicles.",
      backgroundUrl: fallbackHero.backgroundUrl,
      mode: "create",
      worldUuid: null
    });
  };

  const showWorldHero = (world: HubWorld) => {
    applyHero(worldToHero(world));
  };

  const openHeroWorldDetail = () => {
    const selectedWorld = worlds.find((world) => world.world_uuid === hero.worldUuid);
    if (selectedWorld) {
      setDetailWorld(selectedWorld);
    }
  };

  if (detailWorld) {
    return (
      <WorldDetailScreen
        world={detailWorld}
        logoUrl={appAssets.logo}
        onBack={closeWorldDetail}
        onWorldsChanged={() => {
          reloadWorlds().then((nextWorlds) => {
            const updatedWorld = nextWorlds.find((world) => world.world_uuid === detailWorld.world_uuid);
            if (updatedWorld) {
              setDetailWorld(updatedWorld);
              applyHero(worldToHero(updatedWorld));
            }
          });
        }}
      />
    );
  }

  return (
    <main className={`hub hub--${hero.mode}`}>
      <style>{hubFontCss}</style>
      <div className="hub__background" aria-hidden="true">
        {backgroundStack.previousUrl ? (
          <div className="hub__background-layer hub__background-layer--previous" style={{ backgroundImage: `url("${backgroundStack.previousUrl}")` }} />
        ) : null}
        <div
          key={`${backgroundStack.version}-${backgroundStack.currentUrl}`}
          className={`hub__background-layer hub__background-layer--current${backgroundStack.animate ? "" : " hub__background-layer--immediate"}`}
          style={{ backgroundImage: `url("${backgroundStack.currentUrl}")` }}
        />
      </div>
      <div className="hub__shade" />
      <Header />

      <section className="hero" aria-label="Selected world">
        <h1 style={{ fontFamily: hero.fontFamily }}>{hero.title}</h1>
        <p style={{ fontFamily: hero.fontFamily }}>{hero.description}</p>
        <div className="hero__actions">
          {hero.mode === "world" ? (
            <>
              <button type="button" className="primary-action">
                <Compass size={21} aria-hidden="true" />
                <span>Open World</span>
              </button>
              <button type="button" className="secondary-action" onClick={openHeroWorldDetail}>
                <CircleHelp size={22} aria-hidden="true" />
                <span>Info</span>
              </button>
            </>
          ) : (
            <button type="button" className="primary-action">
              <Sparkles size={21} aria-hidden="true" />
              <span>Create</span>
            </button>
          )}
        </div>
      </section>

      <section className="worlds-band" aria-label="Worlds">
        <div className="worlds-row">
          <CreateWorldCard worldCount={worlds.length} onPreview={showCreateHero} />
          {worlds.map((world) => (
            <WorldCard
              key={world.id}
              world={world}
              onPreview={() => showWorldHero(world)}
            />
          ))}
        </div>
      </section>
    </main>
  );
}

function Header() {
  return (
    <header className="topbar">
      <div className="brand" aria-label="VySol">
        <img className="brand__mark" src={appAssets.logo} alt="" />
        <span className="brand__wordmark">VySol</span>
      </div>
      <nav className="nav-pill" aria-label="Main">
        <button type="button" className="nav-pill__item nav-pill__item--active">
          <Compass size={22} aria-hidden="true" />
          <span>Worlds</span>
        </button>
        <span className="nav-pill__divider" />
        <button type="button" className="nav-pill__icon" aria-label="Search">
          <Search size={27} aria-hidden="true" />
        </button>
        <button type="button" className="nav-pill__icon" aria-label="Settings">
          <Settings size={25} aria-hidden="true" />
        </button>
      </nav>
    </header>
  );
}

function CreateWorldCard({ worldCount, onPreview }: { worldCount: number; onPreview: () => void }) {
  return (
    <div className="world-card-shell create-card-shell">
      <span className="world-card__frame" aria-hidden="true" />
      <button type="button" className="create-card" onMouseEnter={onPreview} onFocus={onPreview} onClick={onPreview}>
        <img src={appAssets.createCard} alt="" />
        <span className="card-hover-panel" />
        <span className="create-card__copy">
          <strong>Create New World</strong>
          <span>{worldCount} Worlds</span>
        </span>
      </button>
    </div>
  );
}

function WorldCard({
  world,
  onPreview
}: {
  world: HubWorld;
  onPreview: () => void;
}) {
  return (
    <div className="world-card-shell">
      <span className="world-card__glow" aria-hidden="true" />
      <span className="world-card__frame" aria-hidden="true" />
      <button type="button" className="world-card" onMouseEnter={onPreview} onFocus={onPreview} onClick={onPreview}>
        <img src={absoluteApiUrl(world.card_url)} alt="" />
        <span className="world-card__overlay" />
        <span className="card-hover-panel" />
        <span className="world-card__content">
          <strong>{world.title}</strong>
          <span className="world-card__meta">
            <span>{world.used_last ?? "Used Last: Never"}</span>
            {typeof world.chronicles === "number" ? (
              <span>
                <BookOpen size={15} aria-hidden="true" />
                {world.chronicles} Chronicles
              </span>
            ) : null}
          </span>
        </span>
      </button>
    </div>
  );
}

function useWorlds(): [HubWorld[], React.Dispatch<React.SetStateAction<HubWorld[]>>, () => Promise<HubWorld[]>] {
  const [worlds, setWorlds] = React.useState<HubWorld[]>([]);

  // BLOCK 1: Load Hub worlds from the backend and keep a callable refresh for detail saves
  // WHY: Keeping world discovery backend-owned prevents the browser from needing direct filesystem paths or knowledge of ignored user folders
  const reloadWorlds = React.useCallback(async () => {
    const nextWorlds = await fetchWorlds();
    setWorlds(nextWorlds);
    return nextWorlds;
  }, []);

  React.useEffect(() => {
    let cancelled = false;
    reloadWorlds()
      .then((nextWorlds) => {
        if (!cancelled) {
          setWorlds(nextWorlds);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setWorlds([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [reloadWorlds]);

  return [worlds, setWorlds, reloadWorlds];
}

function worldToHero(world: HubWorld): HeroState {
  return {
    title: world.title,
    description: world.description,
    backgroundUrl: absoluteApiUrl(world.background_url),
    mode: "world",
    worldUuid: world.world_uuid,
    fontFamily: worldFontFamily(world.selected_font)
  };
}

function buildHubFontCss(worlds: HubWorld[]): string {
  const userFontsById = new Map<string, UserAsset>();
  for (const world of worlds) {
    const font = world.selected_font;
    if (font?.url) {
      userFontsById.set(font.id, font);
    }
  }
  return [...userFontsById.values()]
    .map((font) => `@font-face{font-family:${worldFontFamily(font)};src:url("${absoluteApiUrl(font.url)}");font-display:swap;}`)
    .join("\n");
}

function worldFontFamily(font: HubWorld["selected_font"] | undefined): string | undefined {
  if (!font) {
    return undefined;
  }
  if (font.css_family) {
    return font.css_family;
  }
  return `"vysol-user-font-${font.id}"`;
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("VySol frontend root was not found.");
}

// BLOCK 1: Reuse the React root when Vite hot reloads this module in development
// VARS: rootWindow = browser window with the cached React root added by this app
// WHY: Creating a second root on the same DOM node logs a React warning during local UI iteration, even though the production mount is fine
type VysolWindow = Window & { __vysolRoot?: ReturnType<typeof createRoot> };
const rootWindow = window as VysolWindow;
const appRoot = rootWindow.__vysolRoot ?? createRoot(root);
rootWindow.__vysolRoot = appRoot;

appRoot.render(
  <StrictMode>
    <App />
  </StrictMode>
);
