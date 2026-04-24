import type { ModelDefinition } from "../../types";
import { getSharedModel } from "../../shared-catalog";

export const geminiEmbedding2PreviewModel: ModelDefinition = getSharedModel(
  "google/gemini-embedding-2-preview",
)!;
