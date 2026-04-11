# VySol

Local-first graph RAG app for roleplaying in pre-existing fictional worlds with grounded, inspectable lore context.

<p align="center">
  <img src="docs/assets/social.png" alt="VySol preview" width="960">
</p>

![Build](https://img.shields.io/badge/build-local%20checks%20manual-lightgrey)
![License](https://img.shields.io/badge/license-AGPLv3%20%2B%20Commercial-blue)
![Python](https://img.shields.io/badge/python-3.14.3-3776AB)
![Node](https://img.shields.io/badge/node-24-339933)

VySol is built for roleplayers who want one place to ingest canon material, extract entities and relationships, run embeddings, and chat with graph-grounded context without stitching together a pile of separate tools.

## What Problem This Solves

Standard chunk-based RAG often misses critical context in large fictional worlds. It retrieves what looks most similar to the current message, but not always what is most important for canon consistency. In roleplay, that means scenes can lose character truth, world rules, and cause-and-effect details that should always be present.

VySol uses a graph-first approach so retrieval is not just "similar text," but connected context. If a character's traits, combat style, limits, history, and world mechanics are linked, VySol can pull those relationships together during a scene instead of returning thin or isolated fragments.

### Quick Example (Before vs After)

- Before (standard chunking): a sword-focused, impatient character may be retrieved as "in a fight," but their deeper constraints (bad at magic, impulsive decisions, personality logic) are often missing.
- Before (standard chunking): if magic is mentioned, you may get a combat snippet but miss core rules like incantation limits, rank systems, and spell construction steps.
- After (VySol graph retrieval): scenes can include both the immediate moment and the connected canon context, so actions, tone, and outcomes stay more faithful to the world.

## Where To Read Next

- [Quickstart](docs/QUICKSTART.md)
- [Features](docs/FEATURES.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Commercial License](docs/COMMERCIAL%20LICENSE.md)

## License

This project is dual-licensed.

- Open-source use: GNU AGPLv3 ([LICENSE](LICENSE))
- Commercial use: [Commercial License](docs/COMMERCIAL%20LICENSE.md)
