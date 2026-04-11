import { getSettingDefinition } from "./settings";
import { providers } from "./providers";
import { getGoogleSettingDefinition } from "./providers/google-ai-studio/data/settings";
import type {
  ModelDefinition,
  ModelSurface,
  ProviderDefinition,
  RegisteredModel,
  SettingDefinition,
} from "./types";

function getProviderSettingDefinition(
  providerId: string,
  settingId: string,
): SettingDefinition | undefined {
  switch (providerId) {
    case "google":
      return getGoogleSettingDefinition(settingId);
    default:
      return undefined;
  }
}

function registerModels(provider: ProviderDefinition): RegisteredModel[] {
  return provider.models.map((model) => ({
    providerId: provider.id,
    providerDisplayName: provider.displayName,
    apiKeyFilePath: provider.apiKeyFilePath,
    model,
  }));
}

const registeredModels = providers.flatMap(registerModels);

export function listProviders(): readonly ProviderDefinition[] {
  return providers;
}

export function listRegisteredModels(): readonly RegisteredModel[] {
  return registeredModels;
}

export function listModelsForSurface(surface: ModelSurface): readonly RegisteredModel[] {
  return registeredModels.filter((entry) => entry.model.surfaces.includes(surface));
}

export function getProvider(providerId: string): ProviderDefinition | undefined {
  return providers.find((provider) => provider.id === providerId);
}

export function getRegisteredModel(modelId: string): RegisteredModel | undefined {
  return registeredModels.find((entry) => entry.model.id === modelId);
}

export function getModel(modelId: string): ModelDefinition | undefined {
  return getRegisteredModel(modelId)?.model;
}

export function resolveModelSettings(modelId: string): SettingDefinition[] {
  const registeredModel = getRegisteredModel(modelId);

  if (!registeredModel) {
    return [];
  }

  return registeredModel.model.settings.flatMap((settingConfig) => {
    const baseSetting =
      getSettingDefinition(settingConfig.settingId) ??
      getProviderSettingDefinition(registeredModel.providerId, settingConfig.settingId);

    if (!baseSetting) {
      return [];
    }

    return [
      {
        ...baseSetting,
        ...settingConfig,
        id: baseSetting.id,
      },
    ];
  });
}
