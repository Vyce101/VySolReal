import type { ModelDefinition } from "../../types";
import {
  googleChatCapabilities,
  googleChatFeatureSettings,
  googleChatSamplingSettings,
  googleMediaResolutionSetting,
} from "./data/presets";

export const gemma431bItModel: ModelDefinition = {
  id: "google/gemma-4-31b-it",
  displayName: "Gemma 4 31B",
  callName: "gemma-4-31b-it",
  description:
    "Flagship open-weight dense model built for high-quality data-center workloads, with a 256K context window and advanced long-context architecture.",
  surfaces: ["chat"],
  limits: {
    maxContextTokens: 256000,
    maxInputTokens: 256000,
    maxOutputTokens: 65536,
  },
  capabilities: googleChatCapabilities,
  settings: [
    ...googleChatSamplingSettings,
    { settingId: "maxOutputTokens", defaultValue: 32768, max: 65536 },
    ...googleChatFeatureSettings,
    {
      settingId: "thinkingLevel",
      defaultValue: "high",
      options: [
        { value: "minimal", label: "Minimal" },
        { value: "high", label: "High" },
      ],
    },
    googleMediaResolutionSetting,
  ],
};
