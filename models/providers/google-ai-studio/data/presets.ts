import type { ModelCapabilities, ModelSettingConfig } from "../../../types";

export const googleChatCapabilities: ModelCapabilities = {
  multimodalInput: true,
  searchGrounding: true,
  mediaResolution: true,
  safetySettings: true,
  stopSequences: true,
  codeExecution: true,
  structuredOutputs: true,
  functionCalling: true,
  thinking: true,
};

export const googleGeminiChatCapabilities: ModelCapabilities = {
  ...googleChatCapabilities,
  googleMapsGrounding: true,
  urlContext: true,
  fileSearch: true,
};

export const googleChatSamplingSettings: readonly ModelSettingConfig[] = [
  { settingId: "temperature" },
  { settingId: "topP" },
  { settingId: "topK" },
  { settingId: "maxInputTokens", limitKey: "maxInputTokens" },
];

export const googleChatFeatureSettings: readonly ModelSettingConfig[] = [
  { settingId: "enableSearch" },
  { settingId: "codeExecution" },
  { settingId: "harassmentSafety" },
  { settingId: "hateSpeechSafety" },
  { settingId: "sexuallyExplicitSafety" },
  { settingId: "dangerousContentSafety" },
];

export const googleGeminiChatFeatureSettings: readonly ModelSettingConfig[] = [
  ...googleChatFeatureSettings,
  { settingId: "urlContext" },
];

export const googleMediaResolutionSetting: ModelSettingConfig = {
  settingId: "mediaResolution",
};
