import type { ProviderDefinition } from "../../../types";
import { sharedProviders } from "../../../shared-catalog";

export const googleProvider: ProviderDefinition = {
  ...sharedProviders.find((provider) => provider.id === "google")!,
};
