import type { ModelDefinition } from "../../types";
import { getSharedModel } from "../../shared-catalog";

export const gemini31FlashLitePreviewModel: ModelDefinition = getSharedModel(
  "google/gemini-3.1-flash-lite-preview",
)!;
