# System Flow Diagram

This view shows the feature-level data flow. Boxes use exact concept-page titles when a concept page exists.

```mermaid
flowchart TB
    classDef external fill:#142030,color:#E8F7FC,stroke:#0892D0,stroke-dasharray: 5 5;
    classDef process fill:#142030,color:#E8F7FC,stroke:#243447,stroke-width:1px;
    classDef primary fill:#142030,color:#E8F7FC,stroke:#0892D0,stroke-width:3px;
    classDef store fill:#142030,color:#E8F7FC,stroke:#243447,stroke-width:1px;
    classDef added fill:#142030,color:#E8F7FC,stroke:#22C55E,stroke-width:2px;
    classDef legend fill:#142030,color:#E8F7FC,stroke:#243447,stroke-width:1px;

    subgraph Input["Input"]
        direction TB
        User["User"]:::external
        SourceFiles["Source Files"]:::external
        QueryText["Query Text"]:::external
    end

    subgraph Processing["Processing Responsibility"]
        direction TB
        TextSplitting["Text Splitting"]:::primary
        Embeddings["Vector Storage And Chunk Embeddings"]:::process
        ChunkRetrieval["Chunk Retrieval"]:::added
        ModelContext["Model Context"]:::process
    end

    subgraph Storage["Storage Responsibility"]
        direction TB
        WorldStorage[("World Storage")]:::store
        Qdrant[("Qdrant Vector Store")]:::store
    end

    Google["Google AI Studio"]:::external

    User -->|ingestion request| TextSplitting
    SourceFiles -->|source files| TextSplitting
    TextSplitting -->|source copies| WorldStorage
    TextSplitting -->|world metadata| WorldStorage
    TextSplitting -->|chunks| WorldStorage
    TextSplitting -->|chunk manifests| WorldStorage
    WorldStorage -->|chunk text| Embeddings
    Embeddings -->|embedding text| Google
    Google -->|embeddings| Embeddings
    Embeddings -->|vector points| Qdrant
    Embeddings -->|embedding manifests| WorldStorage
    QueryText -->|query text| ChunkRetrieval
    ChunkRetrieval -->|query text| Google
    Google -->|query vector| ChunkRetrieval
    ChunkRetrieval -->|vector query| Qdrant
    Qdrant -->|scored points| ChunkRetrieval
    WorldStorage -->|chunk files| ChunkRetrieval
    ChunkRetrieval -->|chunk text| ModelContext
    ChunkRetrieval -->|rich results| User

    subgraph Legend["Legend"]
        direction TB
        L1["Dashed border: external input or system"]:::external
        L2["Blue border: primary current flow"]:::primary
        L3["Plain border: existing processing"]:::process
        L4["Green border: newly added retrieval feature"]:::added
        L5[("Cylinder: data store")]:::store
    end

    linkStyle 0 stroke:#0892D0;
    linkStyle 1 stroke:#7A90A4;
    linkStyle 2 stroke:#0892D0;
    linkStyle 3 stroke:#0892D0;
    linkStyle 4 stroke:#0892D0;
    linkStyle 5 stroke:#0892D0;
    linkStyle 6 stroke:#0892D0;
    linkStyle 7 stroke:#0892D0;
    linkStyle 8 stroke:#22C55E;
    linkStyle 9 stroke:#22C55E;
    linkStyle 10 stroke:#22C55E;
    linkStyle 11 stroke:#7A90A4;
    linkStyle 12 stroke:#0892D0;
    linkStyle 13 stroke:#22C55E;
    linkStyle 14 stroke:#0892D0;
    linkStyle 15 stroke:#22C55E;
    linkStyle 16 stroke:#0892D0;
    linkStyle 17 stroke:#22C55E;
    linkStyle 18 stroke:#0892D0;
```
