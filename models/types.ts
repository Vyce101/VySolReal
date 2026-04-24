export type SettingValue = string | number | boolean;

export type ModelSurface = "chat" | "embedding";

export type SettingValueType = "number" | "boolean" | "enum" | "string";

export type SettingControlType = "slider" | "toggle" | "select" | "input" | "textarea";

export interface SettingOption {
  value: string;
  label: string;
  description?: string;
}

export interface SettingDefinition {
  id: string;
  displayName: string;
  description: string;
  valueType: SettingValueType;
  control: SettingControlType;
  defaultValue: SettingValue;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  options?: readonly SettingOption[];
}

export interface ModelSettingConfig {
  settingId: string;
  limitKey?: keyof ModelLimitSet;
  defaultValue?: SettingValue;
  min?: number;
  max?: number;
  step?: number;
  options?: readonly SettingOption[];
}

export interface ModelLimitSet {
  maxContextTokens?: number;
  maxInputTokens?: number;
  maxOutputTokens?: number;
  maxEmbeddingDimensions?: number;
}

export interface ModelCapabilities {
  multimodalInput?: boolean;
  searchGrounding?: boolean;
  googleMapsGrounding?: boolean;
  urlContext?: boolean;
  codeExecution?: boolean;
  fileSearch?: boolean;
  mediaResolution?: boolean;
  safetySettings?: boolean;
  stopSequences?: boolean;
  structuredOutputs?: boolean;
  functionCalling?: boolean;
  thinking?: boolean;
}

export interface ModelDefinition {
  id: string;
  displayName: string;
  callName: string;
  description: string;
  surfaces: readonly ModelSurface[];
  limits: ModelLimitSet;
  capabilities?: ModelCapabilities;
  settings: readonly ModelSettingConfig[];
}

export interface ProviderDefinition {
  id: string;
  displayName: string;
  apiKeyFilePath: string;
  settings?: Record<string, SettingDefinition>;
  models: readonly ModelDefinition[];
}

export interface RegisteredModel {
  providerId: string;
  providerDisplayName: string;
  apiKeyFilePath: string;
  providerSettings?: Record<string, SettingDefinition>;
  model: ModelDefinition;
}
