# Model Registry

The model registry is the layer that turns provider and model definition files into the list of models VySol can actually offer in the app.

## Why VySol Uses A Registry Layer

VySol needs more than a hardcoded dropdown of model names. Each model has different jobs it can do, different token limits, different toggles, different safety controls, and sometimes provider-specific settings that do not exist anywhere else. The registry exists so model support can be added as provider-owned metadata instead of being scattered through UI code, request code, and special-case conditionals.

## How A Model Becomes Available

The flow starts with the shared JSON catalog under `models/catalog`. The provider manifest lists each provider's identity, API key path, optional provider setting file, and the model JSON files it owns. The TypeScript registry and the Python backend both read that same catalog, so model constants such as call names, input limits, and embedding dimensions do not need to be copied into two languages.

Each individual model is described in its own JSON file. That definition includes the stable id, the user-facing display name, the provider call name, the current surface it belongs to, the limit values, optional capability flags, and the setting ids the model exposes. VySol currently treats `chat` and `embedding` as the first-class model surfaces. Future extraction should use chat-capable models with the right capabilities, such as structured outputs, instead of becoming a duplicate model category too early.

The registry then auto-populates by flattening every provider's model list into one normalized collection of registered entries. That happens in `models/registry.ts`, where `providers.flatMap(registerModels)` produces the final registry entries. Each entry keeps the provider metadata attached to the model, which means the rest of the app does not need to manually reconnect a model back to its provider later.

From that normalized list, the rest of the system can ask focused questions instead of doing file-level lookups. `listRegisteredModels()` returns everything. `listModelsForSurface(surface)` returns only the models that belong to a current surface such as chat or embeddings. `getModel()` and `getRegisteredModel()` resolve individual models by id. The auto-population part is simple on purpose: if a provider is in the provider manifest and a model file is listed there, it is part of the registry.

Settings are resolved in a second pass. A model file does not redefine every control from scratch. Instead, it references setting ids, and the registry resolves those ids against the shared setting catalog in `models/settings.ts` and then falls back to the setting catalog owned by that model's provider package. After that, any per-model overrides are merged in, which lets a model narrow ranges, change defaults, or limit option sets without duplicating the full definition. Model setting configs can also bind a setting to a model limit, so controls such as max input tokens and embedding dimensions derive their maximum from the model metadata instead of a hardcoded registry special case.

## Why It Is Wired This Way

This structure keeps model support additive. To add a model to an existing provider, add the model JSON file and attach it to that provider in the shared manifest. To add a brand-new provider, add the provider's shared catalog files and, when backend execution is needed, add one backend runtime adapter for that provider. The core registry should not need a provider-specific switch just to understand that provider's settings.

It also keeps the boundaries clean between global behavior and provider-specific behavior. Shared settings such as temperature or output token caps live once in the common catalog. Provider-only controls live with their own provider instead of polluting the global model layer. The registry is the join point that combines those pieces into the final shape the app can use.

Most importantly, it makes the system safer to grow. As VySol adds more providers, more surfaces, and more model-specific controls, the registry keeps the source of truth declarative. Instead of asking the frontend and backend to remember separate copies of which model supports what, both layers derive that information from the same catalog every time.
