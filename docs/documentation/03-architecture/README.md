# Architecture

Placeholder architecture diagram for the app.

```mermaid
flowchart TD
    A["Source Material"] -->|"raw text, files, canon docs"| B["Ingestion"]
    B -->|"source file: path, type, world name"| I["Text Splitting"]
    I -->|"chunk: text, number, overlap, source filename"| J["World Storage"]
    I -->|"progress: total chunks, completed chunks, warnings"| J
```
