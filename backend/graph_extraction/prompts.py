"""Default graph extraction prompt templates."""

from __future__ import annotations

from .models import ExtractionPassRecord, RawExtractedEdge, RawExtractedNode

COMPLETION_MARKER = "---COMPLETE---"

INITIAL_EXTRACTION_PROMPT = """-Goal-
Given a text document that is potentially relevant to this activity, identify all entities from the text and all relationships among the identified entities.

### Inputs
- Chunk body
- Optional reference-only overlap context from the previous chunk

### Constraints
- Use the overlap context only to resolve references such as names, titles, and pronouns inside the chunk body.
- Do not extract entities or relationships that appear only in the overlap context unless the chunk body also supports them.
- Do not create edges to entities that were not extracted as nodes in the same response.
- Return exactly one valid JSON object matching the response schema below.
- Do not wrap the JSON in markdown code fences.
- Do not write any text before the JSON object.
- After the JSON closing brace, write exactly one newline and then ---COMPLETE---.
- Do not write any text after ---COMPLETE---.
- The final characters of the entire response must be ---COMPLETE---.

### Extraction Steps
1. Identify all entities. For each identified entity, extract the following information:
- display_name: The name of the entity, capitalized
- description: Comprehensive description of the entity's attributes and activities

2. From the entities identified in step 1, identify all pairs of entities that are clearly related to each other.
For each pair of related entities, extract the following information:
- source_display_name: name of the source entity, as identified in step 1 (exact name as extracted)
- target_display_name: name of the target entity, as identified in step 1 (exact name as extracted)
- description: a detailed explanation as to why you think the source entity and the target entity are related to each other. Preserve specific actions, motives, context, roles, and important details. Do not use vague labels.
- strength: a numeric score indicating strength of the relationship (not social wise but importance wise) between the source entity and target entity (1-10)

### Start of Response Schema
{
  "nodes": [
    {
      "display_name": str,
      "description": str
    }
  ],
  "edges": [
    {
      "source_display_name": str,
      "target_display_name": str,
      "description": str,
      "strength": int
    }
  ]
}
---COMPLETE---
### End of Response Schema
"""

GLEANING_PROMPT = """Continue extracting for the same chunk after a previous pass.

### Inputs
- Chunk body
- Optional reference-only overlap context from the previous chunk
- Previously extracted entities and relationships for this same chunk

### Constraints
- Use the overlap context only to resolve references such as names, titles, and pronouns inside the chunk body.
- Do not add entities or relationships that appear only in the overlap context unless the chunk body also supports them.
- Use the previously extracted entities and relationships to find genuinely missed entities, missed relationships, and extra relationships between already extracted entities and newly extracted entities when the chunk body supports them.
- Do not repeat entities or relationships that were already extracted.
- Add only new information that is supported by the current chunk body.
- Do not create edges to entities that were not extracted as nodes in the same response.
- Return exactly one valid JSON object matching the response schema below.
- Do not wrap the JSON in markdown code fences.
- Do not write any text before the JSON object.
- After the JSON closing brace, write exactly one newline and then ---COMPLETE---.
- Do not write any text after ---COMPLETE---.
- The final characters of the entire response must be ---COMPLETE---.

### Start of Response Schema
{
  "nodes": [
    {
      "display_name": str,
      "description": str
    }
  ],
  "edges": [
    {
      "source_display_name": str,
      "target_display_name": str,
      "description": str,
      "strength": int
    }
  ]
}
---COMPLETE---
### End of Response Schema
"""


def build_initial_prompt(*, chunk_text: str, overlap_text: str) -> str:
    """Build the default initial extraction prompt."""
    # BLOCK 1: Place the explicit instructions before the overlap and chunk body payloads
    # WHY: The completion marker and display-name schema are user-visible prompt requirements, so they belong in the actual default prompt text sent to the model
    return "\n\n".join(
        [
            INITIAL_EXTRACTION_PROMPT.strip(),
            "### Reference-Only Overlap Context",
            overlap_text.strip() if overlap_text.strip() else "(none)",
            "### Chunk Body",
            chunk_text,
        ]
    )


def build_gleaning_prompt(
    *,
    chunk_text: str,
    overlap_text: str,
    previous_passes: list[ExtractionPassRecord],
    current_nodes: list[RawExtractedNode],
    current_edges: list[RawExtractedEdge],
) -> str:
    """Build the default gleaning prompt from saved previous extraction data."""
    # BLOCK 1: Feed the model the saved prior passes and current merged candidates before asking for missed items
    # WHY: Gleaning depends on previous extraction state, and saving/rehydrating this text lets resume continue after crashes without rerunning trusted earlier calls
    return "\n\n".join(
        [
            GLEANING_PROMPT.strip(),
            "### Reference-Only Overlap Context",
            overlap_text.strip() if overlap_text.strip() else "(none)",
            "### Chunk Body",
            chunk_text,
            "### Previous Passes",
            _format_previous_passes(previous_passes),
            "### Current Merged Nodes",
            _format_nodes(current_nodes),
            "### Current Merged Edges",
            _format_edges(current_edges),
        ]
    )


def _format_previous_passes(previous_passes: list[ExtractionPassRecord]) -> str:
    # BLOCK 1: Render saved pass payloads into compact model-readable text
    # WHY: The next glean only needs trusted parsed extraction state, not the original raw model response body
    if not previous_passes:
        return "(none)"
    return "\n\n".join(
        f"{pass_record.pass_type} {pass_record.pass_number}: nodes={pass_record.nodes} edges={pass_record.edges}"
        for pass_record in previous_passes
    )


def _format_nodes(nodes: list[RawExtractedNode]) -> str:
    # BLOCK 1: Render current nodes with local candidate UUIDs for model context only
    # WHY: UUIDs are backend-owned candidate identities, but including them in context helps the prompt describe current state without asking the model to create ids
    if not nodes:
        return "(none)"
    return "\n".join(
        f"- {node.display_name}: {node.description}"
        for node in nodes
    )


def _format_edges(edges: list[RawExtractedEdge]) -> str:
    # BLOCK 1: Render current edges by display names and descriptions for gleaning context
    # WHY: The model should reason about missed relationships in human terms while the backend keeps UUID endpoint resolution internally
    if not edges:
        return "(none)"
    return "\n".join(
        f"- {edge.source_display_name} -> {edge.target_display_name}: {edge.description} (strength {edge.strength})"
        for edge in edges
    )
