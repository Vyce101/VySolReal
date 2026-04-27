import * as React from "react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BookOpen, CircleHelp, Compass, Search, Settings, Sparkles } from "lucide-react";
import "@fontsource-variable/inter/wght.css";
import "./styles.css";

type HubWorld = {
  id: string;
  slug: string;
  title: string;
  description: string;
  background_url: string;
  card_url: string;
  used_last?: string;
  last_used_at?: string;
  chronicles?: number;
  order: number;
};

type HeroState = {
  title: string;
  description: string;
  backgroundUrl: string;
  mode: "world" | "create" | "empty";
};

type BackgroundStack = {
  previousUrl: string | null;
  currentUrl: string;
  version: number;
};

const API_BASE_URL = "http://127.0.0.1:8000";
const appAssets = {
  logo: `${API_BASE_URL}/api/app-assets/Butterfly_logo.png`,
  defaultWorld: `${API_BASE_URL}/api/app-assets/default_world_image.png`,
  createCard: `${API_BASE_URL}/api/app-assets/create_new_world_button.png`
};
const fallbackHero: HeroState = {
  title: "VySol",
  description: "Create a world, bring in canon material, and build grounded roleplay context.",
  backgroundUrl: appAssets.defaultWorld,
  mode: "empty"
};

function App() {
  const [worlds, setWorlds] = useWorlds();
  const [hero, setHero] = React.useState<HeroState>(fallbackHero);
  const [backgroundStack, setBackgroundStack] = React.useState<BackgroundStack>({
    previousUrl: null,
    currentUrl: fallbackHero.backgroundUrl,
    version: 0
  });

  const applyHero = React.useCallback((nextHero: HeroState) => {
    setHero(nextHero);
    setBackgroundStack((currentStack) => {
      if (currentStack.currentUrl === nextHero.backgroundUrl) {
        return currentStack;
      }
      return {
        previousUrl: currentStack.currentUrl,
        currentUrl: nextHero.backgroundUrl,
        version: currentStack.version + 1
      };
    });
  }, []);

  // BLOCK 1: Pick the first real world as the initial Hub focus after worlds load
  // VARS: firstWorld = first backend world after backend-side ordering
  // WHY: The Hub should open on user content when it exists, but still keep the neutral fallback for an empty install
  React.useEffect(() => {
    const firstWorld = worlds[0];
    if (!firstWorld) {
      applyHero(fallbackHero);
      return;
    }
    applyHero(worldToHero(firstWorld));
  }, [applyHero, worlds]);

  const showCreateHero = () => {
    applyHero({
      title: "Create New World",
      description: "Start a new world space for sources, lore, and future chronicles.",
      backgroundUrl: fallbackHero.backgroundUrl,
      mode: "create"
    });
  };

  const showWorldHero = (world: HubWorld) => {
    applyHero(worldToHero(world));
  };

  return (
    <main className={`hub hub--${hero.mode}`}>
      <div className="hub__background" aria-hidden="true">
        {backgroundStack.previousUrl ? (
          <div className="hub__background-layer hub__background-layer--previous" style={{ backgroundImage: `url("${backgroundStack.previousUrl}")` }} />
        ) : null}
        <div
          key={`${backgroundStack.version}-${backgroundStack.currentUrl}`}
          className="hub__background-layer hub__background-layer--current"
          style={{ backgroundImage: `url("${backgroundStack.currentUrl}")` }}
        />
      </div>
      <div className="hub__shade" />
      <Header />

      <section className="hero" aria-label="Selected world">
        <h1>{hero.title}</h1>
        <p>{hero.description}</p>
        <div className="hero__actions">
          {hero.mode === "world" ? (
            <>
              <button type="button" className="primary-action">
                <Compass size={21} aria-hidden="true" />
                <span>Open World</span>
              </button>
              <button type="button" className="secondary-action">
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

function useWorlds(): [HubWorld[], React.Dispatch<React.SetStateAction<HubWorld[]>>] {
  const [worlds, setWorlds] = React.useState<HubWorld[]>([]);

  // BLOCK 1: Load Hub worlds from the backend once when the app shell starts
  // WHY: Keeping world discovery backend-owned prevents the browser from needing direct filesystem paths or knowledge of ignored user folders
  React.useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/api/worlds`)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`World request failed with ${response.status}`);
        }
        return response.json() as Promise<{ worlds: HubWorld[] }>;
      })
      .then((payload) => {
        if (!cancelled) {
          setWorlds(payload.worlds);
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
  }, []);

  return [worlds, setWorlds];
}

function worldToHero(world: HubWorld): HeroState {
  return {
    title: world.title,
    description: world.description,
    backgroundUrl: absoluteApiUrl(world.background_url),
    mode: "world"
  };
}

function absoluteApiUrl(url: string): string {
  if (url.startsWith("http")) {
    return url;
  }
  return `${API_BASE_URL}${url}`;
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
