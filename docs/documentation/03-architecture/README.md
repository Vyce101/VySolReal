# Architecture

Placeholder architecture diagram for the app.

```mermaid
flowchart TD
    classDef process fill:#0892D0,color:#FFFFFF,stroke:none
    classDef storage fill:#1B2430,color:#E8F7FC,stroke:none
    classDef source fill:#F4F7F9,color:#1B2430,stroke:#D1D9E0

    A([Source Material]):::source
    B[Ingestion]:::process
    C[Text Splitting]:::process
    D[(World Storage)]:::storage

    A -->|source files| B
    B -->|source files| C
    C -->|chunks| D
```
