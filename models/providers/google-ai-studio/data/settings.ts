import type { SettingDefinition } from "../../../types";
import { googleProvider } from "./provider";

export const googleSettingCatalog: Record<string, SettingDefinition> =
  googleProvider.settings ?? {};
