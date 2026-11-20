"""Load and validate the knowledge graph (`graph/graph.json`).

The graph is owned by the ballerina-ecosystem repo. Its schema and checks come
from that repo's `ballerina_graph` package (pinned in shared/pyproject.toml to
the same commit as `graph/SOURCE.json`), so the rules live in one place:

* every node is parsed with the strict Pydantic `Node` model (schema 0.2),
* `validate_graph` runs the graph-level checks (ids, edges, `via`/`target_items`,
  `requires` cycles, deprecated nodes, ...),
* this module adds checks for the exported file itself: schema version, the
  sha256 recorded in SOURCE.json, and top-level edges equal to node edges.

Item references are `node_id#item_id` (`Graph.ref`), the ids every dataset
sample uses in `covers`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from balpipe_common.ids import sha256_text


class GraphError(RuntimeError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"{len(errors)} graph error(s): " + "; ".join(errors[:5]))
        self.errors = errors


@dataclass
class LoadedGraph:
    graph: Any                     # ballerina_graph.Graph
    path: Path
    schema_version: str
    sha256: str
    source: dict[str, Any] = field(default_factory=dict)  # SOURCE.json
    warnings: list[str] = field(default_factory=list)

    @property
    def version(self) -> str:
        """Short id recorded with every run and sample: upstream commit, else file hash."""
        return (self.source.get("commit") or self.sha256)[:12]


def load_graph_json(path: Path, source_file: Path | None = None, *, strict: bool = True) -> LoadedGraph:
    """Parse, validate and return the graph. Raises GraphError when strict and invalid."""
    from ballerina_graph import SCHEMA_VERSION, Graph, Node, validate_graph
    from pydantic import ValidationError

    raw_text = path.read_text(encoding="utf-8")
    data = json.loads(raw_text)
    errors: list[str] = []
    source = json.loads(source_file.read_text(encoding="utf-8")) if source_file and source_file.is_file() else {}
    file_hash = sha256_text(raw_text)

    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version {data.get('schema_version')!r} != supported {SCHEMA_VERSION!r}")
    if source.get("sha256") and source["sha256"] != file_hash:
        errors.append("graph.json does not match the sha256 in SOURCE.json (update SOURCE.json after copying)")

    nodes: dict[str, Any] = {}
    for raw in data.get("nodes", []):
        try:
            node = Node.model_validate(raw)
        except ValidationError as exc:
            errors.append(f"{raw.get('id', '?')}: {exc.errors()[0]['msg']}")
            continue
        if node.id in nodes:
            errors.append(f"duplicate node id {node.id}")
        nodes[node.id] = node

    graph_errors, warnings = validate_graph(nodes)
    errors.extend(graph_errors)

    top = {(e["source"], e["target"], e["rel"]) for e in data.get("edges", [])}
    embedded = {(n.id, e.to, e.rel.value if hasattr(e.rel, "value") else e.rel) for n in nodes.values() for e in n.edges}
    if top != embedded:
        errors.append(f"top-level edges ({len(top)}) differ from node edges ({len(embedded)})")

    if errors and strict:
        raise GraphError(errors)
    return LoadedGraph(Graph(nodes, warnings), path, str(data.get("schema_version")), file_hash, source,
                       warnings + ([f"ERROR: {e}" for e in errors] if not strict else []))


def graph_stats(lg: LoadedGraph) -> dict[str, Any]:
    from collections import Counter

    g = lg.graph
    nodes = list(g.nodes.values())
    items = list(g.iter_items())
    val = lambda x: x.value if hasattr(x, "value") else str(x)  # noqa: E731 - enums or plain strings
    return {
        "version": lg.version,
        "schema_version": lg.schema_version,
        "nodes": len(nodes),
        "items": len(items),  # active items (deprecated nodes excluded), the coverage target
        "items_total": sum(1 for _ in g.iter_items(include_retired=True)),
        "edges": sum(len(n.edges) for n in nodes),
        "node_types": dict(Counter(val(n.type) for n in nodes).most_common()),
        "priority_nodes": dict(Counter(val(n.priority) for n in nodes).most_common()),
        "priority_items": dict(Counter(val(n.priority) for n, _ in items).most_common()),
        "status": dict(Counter(val(n.status) for n in nodes).most_common()),
        "difficulty": dict(Counter(val(d) for _, i in items for d in i.difficulty).most_common()),
        "items_without_own_sources": sum(1 for _, i in items if not i.sources),
        "warnings": len(lg.warnings),
    }
