"""Parsing and validation for LLM graph extraction responses."""

from __future__ import annotations

import json
from uuid import UUID, uuid4, uuid5

from .errors import GraphExtractionParseError
from .models import ExtractionPassRecord, RawExtractedEdge, RawExtractedNode
from .prompts import COMPLETION_MARKER


def parse_extraction_response(response_text: str) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    """Parse a provider response into raw node and edge payloads."""
    # BLOCK 1: Require the explicit completion marker before trusting any JSON-looking response
    # WHY: A model can produce valid partial JSON before being interrupted, so the marker is the app's proof that the model reached the intended end of the response
    marker_index = response_text.find(COMPLETION_MARKER)
    if marker_index < 0:
        raise GraphExtractionParseError(
            code="EXTRACTION_RESPONSE_INCOMPLETE",
            message="The extraction response did not include the completion marker.",
        )

    # BLOCK 2: Decode the JSON object before the marker, allowing common markdown code-fence wrapping
    # WHY: Models often wrap JSON in fences even when asked for JSON only, and accepting that harmless wrapper improves reliability without accepting incomplete output
    json_text = _extract_json_text(response_text[:marker_index].strip())
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise GraphExtractionParseError(
            code="EXTRACTION_RESPONSE_INVALID_JSON",
            message="The extraction response before the completion marker was not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise GraphExtractionParseError(
            code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
            message="The extraction response must be a JSON object.",
        )

    # BLOCK 3: Normalize only schema-valid node and edge dictionaries into the small trusted raw shape
    # WHY: Later merging and UUID assignment should work from predictable Python data instead of provider-shaped arbitrary objects
    nodes = _parse_nodes(payload.get("nodes"))
    edges = _parse_edges(payload.get("edges"))
    return nodes, edges


def merge_pass_records(
    pass_records: list[ExtractionPassRecord],
    *,
    world_uuid: str | None = None,
    ingestion_run_id: str | None = None,
    book_number: int | None = None,
    chunk_number: int | None = None,
) -> tuple[list[RawExtractedNode], list[RawExtractedEdge]]:
    """Merge saved extraction passes into final raw candidates."""
    # BLOCK 1: Merge exact display-name duplicates into one local node candidate with combined descriptions
    # WHY: Fuzzy entity resolution is a future system, but exact duplicate names inside one chunk are safe to collapse so local edge endpoints have one UUID target
    node_order: list[str] = []
    node_descriptions: dict[str, list[str]] = {}
    for pass_record in pass_records:
        for node in pass_record.nodes:
            display_name = node["display_name"].strip()
            description = node["description"].strip()
            if not display_name or not description:
                continue
            if display_name not in node_descriptions:
                node_order.append(display_name)
                node_descriptions[display_name] = []
            if description not in node_descriptions[display_name]:
                node_descriptions[display_name].append(description)

    nodes = [
        RawExtractedNode(
            node_id=_candidate_id(
                world_uuid=world_uuid,
                ingestion_run_id=ingestion_run_id,
                book_number=book_number,
                chunk_number=chunk_number,
                candidate_key=f"node:{display_name}",
            ),
            display_name=display_name,
            description="\n\n".join(node_descriptions[display_name]),
        )
        for display_name in node_order
    ]
    node_by_display_name = {node.display_name: node for node in nodes}

    # BLOCK 2: Keep every valid edge whose display-name endpoints resolve to final local nodes
    # WHY: Slightly different relationship descriptions are useful evidence, but edges to missing or malformed endpoints would create graph records that cannot point at local extracted node candidates
    edges: list[RawExtractedEdge] = []
    valid_edge_index = 0
    for pass_record in pass_records:
        for edge in pass_record.edges:
            source_display_name = str(edge["source_display_name"]).strip()
            target_display_name = str(edge["target_display_name"]).strip()
            source_node = node_by_display_name.get(source_display_name)
            target_node = node_by_display_name.get(target_display_name)
            if source_node is None or target_node is None:
                continue
            strength = edge.get("strength")
            if isinstance(strength, bool) or not isinstance(strength, int) or strength < 1 or strength > 10:
                continue
            description = str(edge["description"]).strip()
            if not description:
                continue
            valid_edge_index += 1
            edges.append(
                RawExtractedEdge(
                    edge_id=_candidate_id(
                        world_uuid=world_uuid,
                        ingestion_run_id=ingestion_run_id,
                        book_number=book_number,
                        chunk_number=chunk_number,
                        candidate_key=(
                            f"edge:{valid_edge_index}:{source_display_name}:"
                            f"{target_display_name}:{description}:{strength}"
                        ),
                    ),
                    source_node_id=source_node.node_id,
                    target_node_id=target_node.node_id,
                    source_display_name=source_display_name,
                    target_display_name=target_display_name,
                    description=description,
                    strength=strength,
                )
            )
    return nodes, edges


def _candidate_id(
    *,
    world_uuid: str | None,
    ingestion_run_id: str | None,
    book_number: int | None,
    chunk_number: int | None,
    candidate_key: str,
) -> str:
    # BLOCK 1: Return deterministic candidate ids only when the run and chunk identity are available
    # VARS: candidate_key = stable local identity for one merged node or kept edge inside the chunk
    # WHY: Manifestation resume needs ids to survive repeated merges, while parser-only unit tests still call this helper without a persisted world context
    if not world_uuid or ingestion_run_id is None or book_number is None or chunk_number is None:
        return str(uuid4())
    return str(
        uuid5(
            UUID(world_uuid),
            f"graph-candidate:{ingestion_run_id}:book:{book_number}:chunk:{chunk_number}:{candidate_key}",
        )
    )


def _extract_json_text(text_before_marker: str) -> str:
    # BLOCK 1: Prefer a fenced JSON body when the model wrapped the response in markdown
    # WHY: This keeps the parser tolerant of common formatting while still requiring one valid JSON object before the completion marker
    stripped = text_before_marker.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    # BLOCK 2: Fall back to the first object-looking slice when harmless prose surrounds the JSON
    # WHY: The completion marker protects against interruption, so trimming non-JSON framing lets the app recover from small model formatting mistakes without accepting arbitrary text
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _parse_nodes(nodes_payload: object) -> list[dict[str, str]]:
    # BLOCK 1: Validate each node has string display and description fields
    # WHY: Missing node names or descriptions cannot become useful graph candidates and should fail the response rather than silently create broken local endpoints
    if not isinstance(nodes_payload, list):
        raise GraphExtractionParseError(
            code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
            message="The extraction response nodes field must be a list.",
        )
    nodes: list[dict[str, str]] = []
    for node_payload in nodes_payload:
        if not isinstance(node_payload, dict):
            raise GraphExtractionParseError(
                code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
                message="Every extracted node must be an object.",
            )
        display_name = node_payload.get("display_name")
        description = node_payload.get("description")
        if not isinstance(display_name, str) or not isinstance(description, str):
            raise GraphExtractionParseError(
                code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
                message="Every extracted node must include string display_name and description fields.",
            )
        nodes.append({"display_name": display_name, "description": description})
    return nodes


def _parse_edges(edges_payload: object) -> list[dict[str, object]]:
    # BLOCK 1: Validate edge objects while leaving strength range enforcement to final candidate validation
    # WHY: A bad strength should drop that edge without discarding otherwise useful nodes or later gleaning data
    if not isinstance(edges_payload, list):
        raise GraphExtractionParseError(
            code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
            message="The extraction response edges field must be a list.",
        )
    edges: list[dict[str, object]] = []
    for edge_payload in edges_payload:
        if not isinstance(edge_payload, dict):
            raise GraphExtractionParseError(
                code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
                message="Every extracted edge must be an object.",
            )
        source_display_name = edge_payload.get("source_display_name")
        target_display_name = edge_payload.get("target_display_name")
        description = edge_payload.get("description")
        if not isinstance(source_display_name, str) or not isinstance(target_display_name, str) or not isinstance(description, str):
            raise GraphExtractionParseError(
                code="EXTRACTION_RESPONSE_INVALID_SCHEMA",
                message="Every extracted edge must include string source_display_name, target_display_name, and description fields.",
            )
        edges.append(
            {
                "source_display_name": source_display_name,
                "target_display_name": target_display_name,
                "description": description,
                "strength": edge_payload.get("strength"),
            }
        )
    return edges
