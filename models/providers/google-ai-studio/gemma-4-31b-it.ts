import type { ModelDefinition } from "../../types";
import { getSharedModel } from "../../shared-catalog";

export const gemma431bItModel: ModelDefinition = getSharedModel("google/gemma-4-31b-it")!;
