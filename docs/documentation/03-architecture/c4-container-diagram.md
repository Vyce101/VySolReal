# C4 Container Diagram

This view shows VySol as runtime containers and data stores. Feature logic such as text splitting, embeddings, and chunk retrieval runs inside the backend container.

```mermaid
C4Container
    title VySol C4 Container Diagram

    Person(user, "User", "Imports source books and asks retrieval-backed questions.")

    System_Boundary(vysol, "VySol") {
        Container(desktop, "VySol Desktop App", "Electron, React", "Desktop interface for ingestion, settings, and future retrieval controls.")
        Container(backend, "Backend", "Python, FastAPI", "Runs ingestion, chunking, embedding, retrieval, and model-context assembly.")
        ContainerDb(worldStorage, "World Storage", "Local file system", "Stores world metadata, source copies, chunk files, progress manifests, and embedding manifests.")
        ContainerDb(qdrantStore, "Qdrant Vector Store", "Qdrant local database", "Stores profile-specific chunk vectors and filterable retrieval payloads.")
    }

    System_Ext(googleAiStudio, "Google AI Studio", "External embedding provider.")

    Rel(user, desktop, "user actions")
    Rel(desktop, backend, "app requests")
    Rel(backend, worldStorage, "world files")
    Rel(backend, googleAiStudio, "text input")
    Rel(googleAiStudio, backend, "embeddings")
    Rel(backend, qdrantStore, "vector points")
    Rel(qdrantStore, backend, "similar chunks")
    Rel(worldStorage, backend, "chunk text")
    Rel(backend, desktop, "retrieval results")
```

## Legend

- Person: someone using VySol.
- Container: a running application inside VySol.
- Database container: a data store owned by VySol.
- External system: a provider outside VySol.
- Relationship labels name the data or request crossing a boundary.
