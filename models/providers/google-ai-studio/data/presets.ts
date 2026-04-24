import type { ModelCapabilities, ModelSettingConfig } from "../../../types";
import { gemini3FlashPreviewModel } from "../gemini-3-flash-preview";
import { gemma431bItModel } from "../gemma-4-31b-it";

export const googleChatCapabilities: ModelCapabilities =
  gemma431bItModel.capabilities ?? {};

export const googleGeminiChatCapabilities: ModelCapabilities =
  gemini3FlashPreviewModel.capabilities ?? {};

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
