import re
import unicodedata


MAX_SOURCE_BYTES = 8192
MAX_NODES = 10
MAX_EDGES = 16
_ID = r"[A-Z][A-Z0-9]{0,7}"
_NODE = re.compile(r'(' + _ID + r')\["([^"\n]*)"\]')
_EDGE = re.compile(r'(' + _ID + r') -->(?:\|"([^"\n]*)"\|)? (' + _ID + r')')


def _label(value, maximum):
    if (any(c in '<>"\\|[]' or unicodedata.category(c).startswith("C")
                   or unicodedata.category(c) in ("Zl", "Zp") for c in value)):
        raise ValueError("Invalid diagram label")
    value = value.strip()
    if not 1 <= len(value) <= maximum or re.search(r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);", value):
        raise ValueError("Invalid diagram label")
    return value


def parse_flowchart(source):
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("Invalid diagram source")
    lines = source.strip().split("\n")
    if lines[0] == "\x60\x60\x60mermaid" and lines[-1] == "\x60\x60\x60":
        lines = lines[1:-1]
    if not lines or lines[0] not in ("flowchart TD", "flowchart LR"):
        raise ValueError("Invalid diagram header")
    nodes, edges = [], []
    ids, pairs = set(), set()
    for line in lines[1:]:
        node = _NODE.fullmatch(line)
        if node and not edges:
            name, label = node.groups()
            if name in ids or len(nodes) >= MAX_NODES:
                raise ValueError("Invalid diagram node")
            ids.add(name)
            nodes.append({"id": name, "label": _label(label, 64)})
            continue
        edge = _EDGE.fullmatch(line)
        if edge is None:
            raise ValueError("Invalid diagram syntax")
        start, label, end = edge.groups()
        if (start not in ids or end not in ids or start == end
                or (start, end) in pairs or len(edges) >= MAX_EDGES):
            raise ValueError("Invalid diagram edge")
        pairs.add((start, end))
        entry = {"from": start, "to": end}
        if label is not None:
            entry["label"] = _label(label, 24)
        edges.append(entry)
    if len(nodes) < 2 or not edges:
        raise ValueError("Incomplete diagram")
    outgoing = {name: set() for name in ids}
    neighbors = {name: set() for name in ids}
    indegree = dict.fromkeys(ids, 0)
    for start, end in pairs:
        outgoing[start].add(end)
        neighbors[start].add(end)
        neighbors[end].add(start)
        indegree[end] += 1
    roots = [name for name in ids if indegree[name] == 0]
    visited = 0
    while roots:
        visited += 1
        for end in outgoing[roots.pop()]:
            indegree[end] -= 1
            if indegree[end] == 0:
                roots.append(end)
    if visited != len(ids):
        raise ValueError("Cyclic diagram")
    connected = {nodes[0]["id"]}
    frontier = list(connected)
    while frontier:
        for end in neighbors[frontier.pop()] - connected:
            connected.add(end)
            frontier.append(end)
    if connected != ids:
        raise ValueError("Disconnected diagram")
    canonical = [lines[0]]
    canonical.extend('{}["{}"]'.format(n["id"], n["label"]) for n in nodes)
    for edge in edges:
        connector = ' -->|"{}"| '.format(edge["label"]) if "label" in edge else " --> "
        canonical.append(edge["from"] + connector + edge["to"])
    mermaid = "\n".join(canonical)
    if len(mermaid.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("Oversized diagram")
    return mermaid, {"direction": lines[0].split()[1], "nodes": nodes, "edges": edges}
