import type { ModelDefinition } from "../../types";

export const gemma431bItModel: ModelDefinition = {
  id: "google/gemma-4-31b-it",
  displayName: "Gemma 4 31B",
  callName: "gemma-4-31b-it",
  description:
    "Flagship open-weight dense model built for high-quality data-center workloads, with a 256K context window and advanced long-context architecture.",
  surfaces: ["chat", "extraction"],
  limits: {
    maxContextTokens: 256000,
    maxInputTokens: 256000,
    maxOutputTokens: 65536,
  },
  capabilities: {
    multimodalInput: true,
    searchGrounding: true,
    mediaResolution: true,
    safetySettings: true,
    stopSequences: true,
    codeExecution: true,
    structuredOutputs: true,
    functionCalling: true,
    thinking: true,
  },
  settings: [
    { settingId: "temperature" },
    { settingId: "topP" },
    { settingId: "topK" },
    { settingId: "maxInputTokens", defaultValue: 256000, max: 256000 },
    { settingId: "maxContextTokens", defaultValue: 256000, max: 256000 },
    { settingId: "maxOutputTokens", defaultValue: 8192, max: 65536 },
    { settingId: "enableSearch" },
    { settingId: "codeExecution" },
    { settingId: "harassmentSafety" },
    { settingId: "hateSpeechSafety" },
    { settingId: "sexuallyExplicitSafety" },
    { settingId: "dangerousContentSafety" },
    {
      settingId: "thinkingLevel",
      defaultValue: "minimal",
      options: [
        { value: "minimal", label: "Minimal" },
        { value: "high", label: "High" },
      ],
    },
    { settingId: "responseMimeType" },
    { settingId: "responseSchemaJson" },
    { settingId: "functionDeclarationsJson" },
    { settingId: "mediaResolution" },
    { settingId: "stopSequences" },
  ],
};
