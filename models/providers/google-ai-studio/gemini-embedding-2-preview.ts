import type { ModelDefinition } from "../../types";

export const geminiEmbedding2PreviewModel: ModelDefinition = {
  id: "google/gemini-embedding-2-preview",
  displayName: "Gemini Embedding 2",
  callName: "gemini-embedding-2-preview",
  description:
    "Preview multimodal embedding model designed for semantic search and retrieval across text, image, video, audio, and document inputs.",
  surfaces: ["embedding"],
  limits: {
    maxInputTokens: 8192,
    maxEmbeddingDimensions: 3072,
  },
  capabilities: {
    structuredOutputs: false,
  },
  settings: [
    { settingId: "maxInputTokens" },
    {
      settingId: "outputDimensionality",
      defaultValue: 3072,
      min: 128,
      max: 3072,
      step: 128,
    },
    { settingId: "embeddingTaskType" },
    { settingId: "embeddingTitle" },
  ],
};
