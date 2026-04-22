# Architecture

This container-level view shows how VySol moves source books into world-backed chunks and then into retrieval-ready vectors.

```mermaid
flowchart LR
    classDef external fill:#142030,color:#E8F7FC,stroke:#0892D0,stroke-dasharray: 5 5;
    classDef process fill:#142030,color:#E8F7FC,stroke:#243447,stroke-width:1px;
    classDef primary fill:#142030,color:#E8F7FC,stroke:#0892D0,stroke-width:3px;
    classDef store fill:#142030,color:#E8F7FC,stroke:#243447,stroke-width:1px;

    subgraph Scope["VySol containers"]
        direction LR

        subgraph Processing["Processing responsibility"]
            TS["Text Splitting"]:::primary
            VCE["Vector Storage And Chunk Embeddings"]:::process
        end

        subgraph Storage["Storage responsibility"]
            WS[("World Storage")]:::store
            QVS[("Qdrant Vector Store")]:::store
        end
    end

    U["User"]:::external
    SF["Source Files"]:::external
    GAI["Google AI Studio"]:::external

    U -->|ingestion request| TS
    SF -->|source files| TS
    TS -->|source copies| WS
    TS -->|world metadata| WS
    TS -->|chunks| WS
    TS -->|chunk manifests| WS
    WS -->|chunk files| VCE
    VCE -->|embedding manifests| WS
    VCE -->|chunk text| GAI
    GAI -->|embeddings| VCE
    VCE -->|vector points| QVS

    linkStyle 0 stroke:#0892D0,color:#E8F7FC;
    linkStyle 1 stroke:#7A90A4,color:#E8F7FC;
    linkStyle 2 stroke:#0892D0,color:#E8F7FC;
    linkStyle 3 stroke:#0892D0,color:#E8F7FC;
    linkStyle 4 stroke:#0892D0,color:#E8F7FC;
    linkStyle 5 stroke:#0892D0,color:#E8F7FC;
    linkStyle 6 stroke:#0892D0,color:#E8F7FC;
    linkStyle 7 stroke:#0892D0,color:#E8F7FC;
    linkStyle 8 stroke:#0892D0,color:#E8F7FC;
    linkStyle 9 stroke:#22C55E,color:#E8F7FC;
    linkStyle 10 stroke:#22C55E,color:#E8F7FC;

    subgraph Legend["Legend"]
        direction TB
        LP["Primary container\nDeep Sky Blue border"]:::primary
        LS[("Data store\nDark Slate border")]:::store
        LE["External actor or system\nDashed border"]:::external
        LN["Primary flow arrow\nDeep Sky Blue"]:::process
        LI["Raw input arrow\nSlate Mist"]:::process
        LC["Confirmed output arrow\nEmerald"]:::process
    end
```
