import type { ModelDefinition } from "../../types";
import { getSharedModel } from "../../shared-catalog";

export const gemini3FlashPreviewModel: ModelDefinition = getSharedModel(
  "google/gemini-3-flash-preview",
)!;
