import type { ModelDefinition } from "../../types";
import {
  googleGeminiChatCapabilities,
  googleGeminiChatFeatureSettings,
  googleChatSamplingSettings,
  googleMediaResolutionSetting,
} from "./data/presets";

export const gemini3FlashPreviewModel: ModelDefinition = {
  id: "google/gemini-3-flash-preview",
  displayName: "Gemini 3 Flash",
  callName: "gemini-3-flash-preview",
  description:
    "Intelligent speed-focused model combining frontier performance with search grounding, structured outputs, and long-context support.",
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
    {
      settingId: "thinkingLevel",
      defaultValue: "high",
      options: [
        { value: "minimal", label: "Minimal" },
        { value: "low", label: "Low" },
        { value: "medium", label: "Medium" },
        { value: "high", label: "High" },
      ],
    },
    googleMediaResolutionSetting,
  ],
};
