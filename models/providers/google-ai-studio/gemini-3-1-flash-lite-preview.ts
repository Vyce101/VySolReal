import type { ModelDefinition } from "../../types";
import {
  googleGeminiChatCapabilities,
  googleGeminiChatFeatureSettings,
  googleChatSamplingSettings,
  googleMediaResolutionSetting,
} from "./data/presets";

export const gemini31FlashLitePreviewModel: ModelDefinition = {
  id: "google/gemini-3.1-flash-lite-preview",
  displayName: "Gemini 3.1 Flash-Lite",
  callName: "gemini-3.1-flash-lite-preview",
  description:
    "Cost-efficient model optimized for high-volume agentic tasks, translation, and simple data processing with multimodal support.",
  surfaces: ["chat"],
  limits: {
    maxContextTokens: 1048576,
    maxInputTokens: 1048576,
    maxOutputTokens: 65536,
  },
  capabilities: googleGeminiChatCapabilities,
  settings: [
    ...googleChatSamplingSettings,
    { settingId: "maxOutputTokens", defaultValue: 65536, max: 65536 },
    ...googleGeminiChatFeatureSettings,
    { settingId: "thinkingLevel", defaultValue: "high" },
    googleMediaResolutionSetting,
  ],
};
