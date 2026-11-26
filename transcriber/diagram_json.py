import json
import re

from .diagram_parser import MAX_EDGES, MAX_NODES, MAX_SOURCE_BYTES, _label, parse_flowchart


INPUT_VERSION = "json-object-v1"
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
    if any(char in raw[:start] for char in "}[]"):
        raise DiagramInputError("ambiguous_envelope")
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
    if any(char in raw[end:] for char in "{}[]"):
        raise DiagramInputError("ambiguous_envelope")
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


def parse_diagram_json(raw):
    graph = extract_object(raw)
    if not isinstance(graph, dict) or set(graph) != {"direction", "nodes", "edges"}:
        raise DiagramInputError("invalid_schema")
    direction, nodes, edges = graph["direction"], graph["nodes"], graph["edges"]
    if direction not in ("TD", "LR") or not isinstance(nodes, list) or not isinstance(edges, list):
        raise DiagramInputError("invalid_schema")
    if not 2 <= len(nodes) <= MAX_NODES or not 1 <= len(edges) <= MAX_EDGES:
        raise DiagramInputError("invalid_cardinality")
    lines = ["flowchart " + direction]
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "label"}:
            raise DiagramInputError("invalid_node")
        name = _node_id(node["id"])
        label = _safe_label(node["label"], 64)
        lines.append('{}["{}"]'.format(name, label))
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) not in ({"from", "to"}, {"from", "to", "label"}):
            raise DiagramInputError("invalid_edge")
        start, end = _node_id(edge["from"]), _node_id(edge["to"])
        connector = ' -->|"{}"| '.format(_safe_label(edge["label"], 24)) if "label" in edge else " --> "
        lines.append(start + connector + end)
    try:
        return parse_flowchart("\n".join(lines))
    except (ValueError, UnicodeError):
        raise DiagramInputError("invalid_graph") from None
