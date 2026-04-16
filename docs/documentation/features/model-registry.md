# Model Registry

The model registry is the layer that turns provider and model definition files into the list of models VySol can actually offer in the app.

## Why VySol Uses A Registry Layer

VySol needs more than a hardcoded dropdown of model names. Each model has different jobs it can do, different token limits, different toggles, different safety controls, and sometimes provider-specific settings that do not exist anywhere else. The registry exists so that model support can be added as data instead of being scattered through UI code, request code, and special-case conditionals. It also exists to make future expansion easier, because adding new providers and new models should feel like extending a system, not rewriting one.

## How A Model Becomes Available

The flow starts with provider definition files. Each provider exports a `ProviderDefinition` object with its identity, display name, API key path, and the list of models it owns. Right now that provider list is assembled in [`models/providers/index.ts`](C:/Coding%20Projects/Apps/VySol/models/providers/index.ts), and the Google provider pulls together its model definitions in [`models/providers/google-ai-studio/data/provider.ts`](C:/Coding%20Projects/Apps/VySol/models/providers/google-ai-studio/data/provider.ts).

Each individual model is described in its own file as a `ModelDefinition`. That definition includes the stable id, the user-facing display name, the provider call name, the surfaces it belongs to such as `chat`, `embedding`, or `extraction`, the limit values, optional capability flags, and the setting ids the model exposes. Files like [`gemini-3-flash-preview.ts`](C:/Coding%20Projects/Apps/VySol/models/providers/google-ai-studio/gemini-3-flash-preview.ts) and [`gemma-4-31b-it.ts`](C:/Coding%20Projects/Apps/VySol/models/providers/google-ai-studio/gemma-4-31b-it.ts) are the source of truth for that metadata.

The registry then auto-populates by flattening every provider's model list into one normalized collection of registered entries. That happens in [`models/registry.ts`](C:/Coding%20Projects/Apps/VySol/models/registry.ts), where `providers.flatMap(registerModels)` produces the final registry entries. Each entry keeps the provider metadata attached to the model, which means the rest of the app does not need to manually reconnect a model back to its provider later.

From that normalized list, the rest of the system can ask focused questions instead of doing file-level lookups. `listRegisteredModels()` returns everything. `listModelsForSurface(surface)` returns only the models that belong to a specific workflow such as embeddings or chat. `getModel()` and `getRegisteredModel()` resolve individual models by id. The auto-population part is simple on purpose: if a provider is in the provider list and a model is in that provider's model array, it is part of the registry.

Settings are resolved in a second pass. A model file does not redefine every control from scratch. Instead, it references setting ids, and the registry resolves those ids against the shared setting catalog in [`models/settings.ts`](C:/Coding%20Projects/Apps/VySol/models/settings.ts) and then falls back to provider-specific catalogs such as [`models/providers/google-ai-studio/data/settings.ts`](C:/Coding%20Projects/Apps/VySol/models/providers/google-ai-studio/data/settings.ts). After that, any per-model overrides are merged in, which lets a model narrow ranges, change defaults, or limit option sets without duplicating the full definition.

## Why It Is Wired This Way

This structure keeps model support additive. To add a model, VySol does not need a new switch statement in every surface and it does not need the UI to know provider internals. A new model can be introduced as a definition file, attached to a provider, and then discovered by the registry automatically. The same pattern applies when new providers are introduced, which is important because the system is meant to grow beyond the providers that happen to exist today.

It also keeps the boundaries clean between global behavior and provider-specific behavior. Shared settings such as temperature or output token caps live once in the common catalog. Provider-only controls live with their own provider instead of polluting the global model layer. The registry is the join point that combines those pieces into the final shape the app can use. Google-specific settings are one current example, but the design is not centered on Google. It is centered on the idea that every provider can bring its own metadata while still fitting the same registry contract.

Most importantly, it makes the system safer to grow. As VySol adds more providers, more surfaces, and more model-specific controls, the registry keeps the source of truth declarative. Instead of asking the rest of the codebase to remember which model supports what, the app can derive that information from the registry every time.
