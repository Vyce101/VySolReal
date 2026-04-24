import { getSettingDefinition } from "./settings";
import { providers } from "./providers";
import type {
  ModelDefinition,
  ModelSurface,
  ProviderDefinition,
  RegisteredModel,
  SettingDefinition,
} from "./types";

function registerModels(provider: ProviderDefinition): RegisteredModel[] {
  return provider.models.map((model) => ({
    providerId: provider.id,
    providerDisplayName: provider.displayName,
    apiKeyFilePath: provider.apiKeyFilePath,
    providerSettings: provider.settings,
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

function applyModelLimitOverrides(
  setting: SettingDefinition,
  model: ModelDefinition,
  limitKey?: keyof ModelDefinition["limits"],
): SettingDefinition {
  if (!limitKey) {
    return setting;
  }

  const limitValue = model.limits[limitKey];

  if (limitValue === undefined) {
    return setting;
  }

  return {
    ...setting,
    defaultValue: limitValue,
    max: limitValue,
  };
}

export function resolveModelSettings(modelId: string): SettingDefinition[] {
  const registeredModel = getRegisteredModel(modelId);

  if (!registeredModel) {
    return [];
  }

  return registeredModel.model.settings.flatMap((settingConfig) => {
    const { limitKey, ...settingOverrides } = settingConfig;
    const baseSetting =
      getSettingDefinition(settingOverrides.settingId) ??
      registeredModel.providerSettings?.[settingOverrides.settingId];

    if (!baseSetting) {
      return [];
    }

    return [
      applyModelLimitOverrides(
        {
          ...baseSetting,
          ...settingOverrides,
          id: baseSetting.id,
        },
        registeredModel.model,
        limitKey,
      ),
    ];
  });
}
