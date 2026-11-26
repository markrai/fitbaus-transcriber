import json
import re

from .diagram_parser import MAX_EDGES, MAX_NODES, MAX_SOURCE_BYTES, _label, parse_flowchart


INPUT_VERSION = "compact-json-v2"
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


def _compact_graph(graph):
    nodes, edges = graph["nodes"], graph["edges"]
    if not all(isinstance(node, str) for node in nodes):
        raise DiagramInputError("mixed_node_formats")
    labels = {chr(65 + index): _safe_label(node, 64) for index, node in enumerate(nodes)}
    links = []
    for edge in edges:
        if not isinstance(edge, list) or len(edge) not in (2, 3):
            raise DiagramInputError("invalid_index_edge")
        start, end = edge[:2]
        if type(start) is not int or type(end) is not int:
            raise DiagramInputError("invalid_edge_index")
        label = _safe_label(edge[2], 24) if len(edge) == 3 else None
        if not 1 <= start <= len(nodes) or not 1 <= end <= len(nodes):
            raise DiagramInputError("unknown_endpoint")
        links.append((chr(64 + start), chr(64 + end), label))
    return labels, links


def _named_graph(graph):
    labels, links = {}, []
    for node in graph["nodes"]:
        if not isinstance(node, dict):
            raise DiagramInputError("invalid_node_type")
        if set(node) != {"id", "label"}:
            raise DiagramInputError("invalid_node_fields")
        name = _node_id(node["id"])
        label = _safe_label(node["label"], 64)
        if name in labels and labels[name] != label:
            raise DiagramInputError("conflicting_node")
        labels[name] = label
    for edge in graph["edges"]:
        if not isinstance(edge, dict) or set(edge) not in ({"from", "to"}, {"from", "to", "label"}):
            raise DiagramInputError("invalid_edge_fields")
        start, end = _node_id(edge["from"]), _node_id(edge["to"])
        label = _safe_label(edge["label"], 24) if "label" in edge else None
        if start not in labels or end not in labels:
            raise DiagramInputError("unknown_endpoint")
        links.append((start, end, label))
    return labels, links


def _normalize(labels, links):
    unique = {}
    for start, end, label in links:
        if start == end:
            continue
        pair = (start, end)
        if pair in unique and unique[pair] != label:
            raise DiagramInputError("conflicting_edge")
        unique[pair] = label
    if not unique:
        raise DiagramInputError("no_usable_edges")
    used = {node for pair in unique for node in pair}
    labels = {name: label for name, label in labels.items() if name in used}
    return labels, unique


def parse_diagram_json(raw):
    graph = extract_object(raw)
    if not isinstance(graph, dict) or set(graph) not in ({"nodes", "edges"}, {"direction", "nodes", "edges"}):
        raise DiagramInputError("invalid_schema")
    direction = graph.get("direction", "TD")
    nodes, edges = graph["nodes"], graph["edges"]
    if direction not in ("TD", "LR") or not isinstance(nodes, list) or not isinstance(edges, list):
        raise DiagramInputError("invalid_schema")
    if not 2 <= len(nodes) <= MAX_NODES or not 1 <= len(edges) <= MAX_EDGES:
        raise DiagramInputError("invalid_cardinality")
    if isinstance(nodes[0], str):
        labels, links = _compact_graph(graph)
    else:
        labels, links = _named_graph(graph)
    labels, unique = _normalize(labels, links)
    lines = ["flowchart " + direction]
    for name, label in labels.items():
        lines.append('{}["{}"]'.format(name, label))
    for (start, end), label in unique.items():
        connector = ' -->|"{}"| '.format(label) if label is not None else " --> "
        lines.append(start + connector + end)
    try:
        return parse_flowchart("\n".join(lines))
    except ValueError as error:
        reason = {"Cyclic diagram": "cyclic_graph", "Disconnected diagram": "disconnected_graph"}.get(
            str(error), "invalid_graph")
        raise DiagramInputError(reason) from None
    except UnicodeError:
        raise DiagramInputError("invalid_graph") from None
