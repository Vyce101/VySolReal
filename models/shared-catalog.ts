import type { ModelDefinition, ProviderDefinition, SettingDefinition } from "./types";

import providersManifest from "./catalog/providers.json";
import googleSettings from "./catalog/providers/google-ai-studio/settings.json";
import gemini3FlashPreview from "./catalog/providers/google-ai-studio/models/gemini-3-flash-preview.json";
import gemini31FlashLitePreview from "./catalog/providers/google-ai-studio/models/gemini-3-1-flash-lite-preview.json";
import gemma431bIt from "./catalog/providers/google-ai-studio/models/gemma-4-31b-it.json";
import geminiEmbedding2Preview from "./catalog/providers/google-ai-studio/models/gemini-embedding-2-preview.json";

interface ProviderManifestEntry {
  id: string;
  displayName: string;
  apiKeyFilePath: string;
  settingsFile?: string;
  modelFiles: readonly string[];
}

interface ProvidersManifest {
  providers: readonly ProviderManifestEntry[];
}

type JsonModule = { default: unknown } | unknown;

interface GlobImportMeta extends ImportMeta {
  glob?: (
    pattern: string,
    options: { eager: true },
  ) => Record<string, JsonModule>;
}

const fallbackCatalogFiles: Record<string, unknown> = {
  "./catalog/providers.json": providersManifest,
  "./catalog/providers/google-ai-studio/settings.json": googleSettings,
  "./catalog/providers/google-ai-studio/models/gemini-3-flash-preview.json": gemini3FlashPreview,
  "./catalog/providers/google-ai-studio/models/gemini-3-1-flash-lite-preview.json": gemini31FlashLitePreview,
  "./catalog/providers/google-ai-studio/models/gemma-4-31b-it.json": gemma431bIt,
  "./catalog/providers/google-ai-studio/models/gemini-embedding-2-preview.json": geminiEmbedding2Preview,
};

const viteCatalogFiles =
  typeof (import.meta as GlobImportMeta).glob === "function"
    ? (import.meta as GlobImportMeta).glob!("./catalog/**/*.json", { eager: true })
    : {};

const catalogFiles: Record<string, unknown> = {
  ...fallbackCatalogFiles,
  ...Object.fromEntries(
    Object.entries(viteCatalogFiles).map(([path, module]) => [
      path,
      module && typeof module === "object" && "default" in module ? module.default : module,
    ]),
  ),
};

function resolveCatalogFile<T>(catalogPath: string): T {
  const file = catalogFiles[`./catalog/${catalogPath}`];

  if (!file) {
    throw new Error(`Shared model catalog is missing ${catalogPath}.`);
  }

  return file as T;
}

function resolveProviderSettings(settingsFile?: string): Record<string, SettingDefinition> | undefined {
  if (!settingsFile) {
    return undefined;
  }

  return resolveCatalogFile<Record<string, SettingDefinition>>(settingsFile);
}

function resolveProviderModels(modelFilePaths: readonly string[]): readonly ModelDefinition[] {
  return modelFilePaths.map((modelFilePath) =>
    resolveCatalogFile<ModelDefinition>(modelFilePath),
  );
}

export const sharedProviders: readonly ProviderDefinition[] = (
  resolveCatalogFile<ProvidersManifest>("providers.json")
).providers.map((provider) => ({
  id: provider.id,
  displayName: provider.displayName,
  apiKeyFilePath: provider.apiKeyFilePath,
  settings: resolveProviderSettings(provider.settingsFile),
  models: resolveProviderModels(provider.modelFiles),
}));

export function getSharedModel(modelId: string): ModelDefinition | undefined {
  return sharedProviders
    .flatMap((provider) => provider.models)
    .find((model) => model.id === modelId);
}
