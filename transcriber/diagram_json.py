from dataclasses import dataclass
import json
import re

from .diagram_parser import MAX_EDGES, MAX_NODES, MAX_SOURCE_BYTES, _label, parse_flowchart


INPUT_VERSION = "normalized-json-v3"
MAX_INPUT_NODES = 24
MAX_INPUT_EDGES = 48
MAX_DEPTH = 8
_ID = re.compile(r"[A-Z][A-Z0-9]{0,7}")


class DiagramInputError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiagramInputError("duplicate_key")
        result[key] = value
    return result


def _constant(_):
    raise DiagramInputError("invalid_json")


def _prose_envelope(text):
    depth = 0
    for char in text:
        if char in "{}":
            raise DiagramInputError("multiple_or_extra_objects")
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise DiagramInputError("array_wrapper_or_unbalanced_prose")
    if depth:
        raise DiagramInputError("array_wrapper_or_unbalanced_prose")


def extract_object(raw):
    if not isinstance(raw, str):
        raise DiagramInputError("invalid_input")
    try:
        size = len(raw.encode("utf-8"))
    except UnicodeError:
        raise DiagramInputError("invalid_unicode") from None
    if size > MAX_SOURCE_BYTES:
        raise DiagramInputError("input_too_large")
    start = raw.find("{")
    if start < 0:
        raise DiagramInputError("missing_object")
    _prose_envelope(raw[:start])
    stack = []
    quoted = escaped = False
    end = None
    for index in range(start, len(raw)):
        char = raw[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            stack.append(char)
            if len(stack) > MAX_DEPTH:
                raise DiagramInputError("excessive_depth")
        elif char in "}]":
            if not stack or stack.pop() != ("{" if char == "}" else "["):
                raise DiagramInputError("invalid_json")
            if not stack:
                end = index + 1
                break
    if end is None:
        raise DiagramInputError("incomplete_object")
    _prose_envelope(raw[end:])
    try:
        return json.loads(raw[start:end], object_pairs_hook=_unique, parse_constant=_constant)
    except DiagramInputError:
        raise
    except (ValueError, RecursionError):
        raise DiagramInputError("invalid_json") from None


def _safe_label(value, limit):
    if not isinstance(value, str):
        raise DiagramInputError("invalid_label")
    try:
        return _label(value, limit)
    except ValueError:
        raise DiagramInputError("invalid_label") from None


def _node_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise DiagramInputError("invalid_node_id")
    return value


@dataclass(frozen=True)
class _DecodedGraph:
    direction: str
    nodes: tuple[tuple[int | str, str], ...]
    edges: tuple[tuple[int | str, int | str, str | None], ...]


@dataclass(frozen=True)
class NormalizationStats:
    input_nodes: int
    input_edges: int
    removed_duplicate_nodes: int
    removed_unknown_edges: int
    removed_duplicate_edges: int
    removed_self_loops: int
    removed_isolated_nodes: int
    output_nodes: int
    output_edges: int

    @property
    def changed(self):
        return self.input_nodes != self.output_nodes or self.input_edges != self.output_edges


@dataclass(frozen=True)
class DiagramResult:
    mermaid: str
    graph: dict
    stats: NormalizationStats


def _compact_graph(direction, nodes, edges):
    if not all(isinstance(node, str) for node in nodes):
        raise DiagramInputError("mixed_node_formats")
    labels = tuple((index, _safe_label(node, 64)) for index, node in enumerate(nodes, 1))
    links = []
    for edge in edges:
        if not isinstance(edge, list) or len(edge) not in (2, 3):
            raise DiagramInputError("invalid_index_edge")
        start, end = edge[:2]
        if type(start) is not int or type(end) is not int:
            raise DiagramInputError("invalid_edge_index")
        label = _safe_label(edge[2], 24) if len(edge) == 3 else None
        if start == 0 or end == 0:
            raise DiagramInputError("ambiguous_index_base")
        if start < 0 or end < 0:
            raise DiagramInputError("invalid_edge_index")
        links.append((start, end, label))
    return _DecodedGraph(direction, labels, tuple(links))


def _named_graph(direction, nodes, edges):
    labels, links = [], []
    for node in nodes:
        if not isinstance(node, dict):
            raise DiagramInputError("invalid_node_type")
        if set(node) != {"id", "label"}:
            raise DiagramInputError("invalid_node_fields")
        labels.append((_node_id(node["id"]), _safe_label(node["label"], 64)))
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) not in ({"from", "to"}, {"from", "to", "label"}):
            raise DiagramInputError("invalid_edge_fields")
        start, end = _node_id(edge["from"]), _node_id(edge["to"])
        label = _safe_label(edge["label"], 24) if "label" in edge else None
        links.append((start, end, label))
    return _DecodedGraph(direction, tuple(labels), tuple(links))


def _decode(raw):
    graph = extract_object(raw)
    if not isinstance(graph, dict) or set(graph) not in ({"nodes", "edges"}, {"direction", "nodes", "edges"}):
        raise DiagramInputError("invalid_schema")
    direction = graph.get("direction", "TD")
    nodes, edges = graph["nodes"], graph["edges"]
    if direction not in ("TD", "LR") or not isinstance(nodes, list) or not isinstance(edges, list):
        raise DiagramInputError("invalid_schema")
    if not 2 <= len(nodes) <= MAX_INPUT_NODES or not 1 <= len(edges) <= MAX_INPUT_EDGES:
        raise DiagramInputError("invalid_cardinality")
    if isinstance(nodes[0], str):
        return _compact_graph(direction, nodes, edges)
    return _named_graph(direction, nodes, edges)


def _normalize(decoded):
    labels = {}
    duplicate_nodes = 0
    for name, label in decoded.nodes:
        if name in labels:
            if labels[name] != label:
                raise DiagramInputError("conflicting_node")
            duplicate_nodes += 1
        else:
            labels[name] = label

    unique = {}
    unknown_edges = duplicate_edges = self_loops = 0
    for start, end, label in decoded.edges:
        if start not in labels or end not in labels:
            unknown_edges += 1
            continue
        if start == end:
            self_loops += 1
            continue
        pair = (start, end)
        if pair in unique:
            if unique[pair] != label:
                raise DiagramInputError("conflicting_edge")
            duplicate_edges += 1
        else:
            unique[pair] = label
    if not unique:
        raise DiagramInputError("no_usable_edges")
    used = {node for pair in unique for node in pair}
    isolated_nodes = len(labels) - len(used)
    labels = {name: label for name, label in labels.items() if name in used}
    if not 2 <= len(labels) <= MAX_NODES or not 1 <= len(unique) <= MAX_EDGES:
        raise DiagramInputError("output_cardinality")
    stats = NormalizationStats(
        len(decoded.nodes), len(decoded.edges), duplicate_nodes, unknown_edges,
        duplicate_edges, self_loops, isolated_nodes, len(labels), len(unique),
    )
    return labels, unique, stats


def parse_diagram_result(raw):
    decoded = _decode(raw)
    labels, unique, stats = _normalize(decoded)
    names = {name: chr(64 + name) if isinstance(name, int) else name for name in labels}
    lines = ["flowchart " + decoded.direction]
    for name, label in labels.items():
        lines.append('{}["{}"]'.format(names[name], label))
    for (start, end), label in unique.items():
        connector = ' -->|"{}"| '.format(label) if label is not None else " --> "
        lines.append(names[start] + connector + names[end])
    try:
        mermaid, graph = parse_flowchart("\n".join(lines))
    except (ValueError, UnicodeError) as error:
        reason = {"Cyclic diagram": "cyclic_graph", "Disconnected diagram": "disconnected_graph"}.get(
            str(error), "invalid_graph")
        raise DiagramInputError(reason) from None
    return DiagramResult(mermaid, graph, stats)


def parse_diagram_json(raw):
    result = parse_diagram_result(raw)
    return result.mermaid, result.graph
