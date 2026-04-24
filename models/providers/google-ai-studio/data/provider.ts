import type { ModelDefinition, ProviderDefinition } from "../../types";
import { gemini3FlashPreviewModel } from "../gemini-3-flash-preview";
import { gemini31FlashLitePreviewModel } from "../gemini-3-1-flash-lite-preview";
import { gemma431bItModel } from "../gemma-4-31b-it";
import { geminiEmbedding2PreviewModel } from "../gemini-embedding-2-preview";
import { googleSettingCatalog } from "./settings";

const googleModels: readonly ModelDefinition[] = [
  gemini3FlashPreviewModel,
  gemini31FlashLitePreviewModel,
  gemma431bItModel,
  geminiEmbedding2PreviewModel,
];

export const googleProvider: ProviderDefinition = {
  id: "google",
  displayName: "Google AI Studio",
  apiKeyFilePath: "user/keys/google-ai-studio",
  settings: googleSettingCatalog,
  models: googleModels,
};
