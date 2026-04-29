<div align="center">

# VySol Documentation

[![VySol butterfly logo](https://raw.githubusercontent.com/Vyce101/VySolReal/main/docs/assets/Butterfly-logo-with-background.png)](https://raw.githubusercontent.com/Vyce101/VySolReal/main/docs/assets/Butterfly-logo-with-background.png)

VySol documentation explains how to run, understand, and safely change the local-first graph RAG app for fictional-world roleplay.

</div>

These docs contain setup guidance, system concepts, and architecture notes for the current main branch. They are meant to help users run VySol, understand what the app is doing, and give future contributors or AI coding agents enough context to change the code without guessing.

## Choose a Path

- [I want to run VySol](https://github.com/Vyce101/VySolReal/blob/main/docs/QUICKSTART.md)
- [I want to understand VySol](02-features/README.md)
- [I want to configure ingestion/retrieval](01-guides/README.md)
- [I want to understand the architecture](03-architecture/README.md)
- [I want to contribute to the codebase](03-architecture/README.md)

## Use VySol

Use this path if you want to install, launch, or configure VySol without needing to understand the internals first.

Guides are user-facing how-to pages. They should explain what to do, keep decisions simple, and avoid turning into architecture notes.

[Open the guides](01-guides/README.md)

## Learn the Core

Use this path if you want to understand how VySol systems work before changing settings, debugging behavior, or editing code.

Concept pages explain system contracts: what each system owns, what it does not own, how normal flow works, which systems it touches, and which invariants must not be broken.

[Open the concepts](02-features/README.md)

## Learn the Architecture

Use this path if you want the larger shape of VySol rather than one feature at a time.

The ADR section will hold architecture decision records. Use it when you need to understand why a major decision was chosen over serious alternatives.

The codebase section will explain where the major parts of the repo live. Use it before making broad code changes or onboarding a new AI coding agent.

[Open the architecture docs](03-architecture/README.md)
