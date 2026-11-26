import copy
import json
import unittest

from transcriber.diagram_json import DiagramInputError, extract_object, parse_diagram_json
from transcriber.diagram_parser import parse_flowchart


GRAPH = {
    "direction": "TD",
    "nodes": [{"id": "A", "label": "Exercise"}, {"id": "B", "label": "Heart health"},
              {"id": "C", "label": "Endurance"}],
    "edges": [{"from": "A", "to": "B"}, {"from": "B", "to": "C", "label": "supports"}],
}


class DiagramJsonTests(unittest.TestCase):
    def test_plain_fenced_and_preamble_responses_have_identical_graph(self):
        encoded = json.dumps(GRAPH)
        canonical, graph = parse_diagram_json(encoded)
        self.assertEqual(graph, GRAPH)
        self.assertEqual(parse_flowchart(canonical), (canonical, graph))
        for value in ("Here is the graph:\n" + encoded,
                      "\x60\x60\x60json\n" + encoded + "\n\x60\x60\x60",
                      "Certainly.\n\x60\x60\x60json\n" + encoded + "\n\x60\x60\x60\nThis is the explanation.",
                      "\n" + encoded + "\n"):
            with self.subTest(value=value):
                self.assertEqual(parse_diagram_json(value), (canonical, graph))

    def test_string_aware_extraction_handles_escaped_quotes_braces_and_backslashes(self):
        graph = {"text": 'a } [ { ] "quoted" \\ path', "nested": [{"v": "{"}]}
        self.assertEqual(extract_object("Here:\n" + json.dumps(graph) + "\nDone."), graph)
        value = copy.deepcopy(GRAPH)
        value["nodes"][0]["label"] = "Set {A, B}"
        canonical, normalized = parse_diagram_json(json.dumps(value))
        self.assertEqual(normalized, value)
        self.assertEqual(parse_flowchart(canonical)[1], value)

    def test_ambiguous_malformed_and_oversized_envelopes_fail_closed(self):
        encoded = json.dumps(GRAPH)
        cases = [
            ("No JSON here.", "missing_object"),
            (encoded + "\n" + encoded, "multiple_or_extra_objects"),
            ("[" + encoded + "]", "array_wrapper_or_unbalanced_prose"),
            ("{} " + encoded, "multiple_or_extra_objects"),
            (encoded + " {", "multiple_or_extra_objects"),
            (encoded[:-1], "incomplete_object"),
            ('{"x": [}', "invalid_json"),
            ('{"x": NaN}', "invalid_json"),
            ('{"x": Infinity}', "invalid_json"),
            ('{"x":1,"x":2}', "duplicate_key"),
            ('{"x":{"a":1,"a":2}}', "duplicate_key"),
            ('{"x":' + "[" * 9 + "0" + "]" * 9 + "}", "excessive_depth"),
            ("x" * 8193, "input_too_large"),
            ("現" * 3000 + encoded, "input_too_large"),
            ("\ud800", "invalid_unicode"),
        ]
        for raw, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(DiagramInputError, "^" + reason + "$"):
                extract_object(raw)

    def test_exact_schema_and_strict_graph_safety_are_preserved(self):
        cases = [
            {**GRAPH, "extra": True}, {**GRAPH, "direction": "BT"}, {**GRAPH, "direction": []},
            {**GRAPH, "nodes": "nodes"}, {**GRAPH, "edges": []},
            {**GRAPH, "nodes": GRAPH["nodes"] + [{"id": "A", "label": "Duplicate"}]},
            {**GRAPH, "edges": GRAPH["edges"] + [{"from": "C", "to": "A"}]},
            {**GRAPH, "edges": GRAPH["edges"] + [{"from": "A", "to": "Z"}]},
            {**GRAPH, "edges": [{"from": "A", "to": "B", "label": None}]},
            {**GRAPH, "edges": [{"from": "A", "to": "B", "style": "red"}]},
        ]
        for bad in (None, 1, [], "<script>", "&lt;", "a\u202eb", "a\tb", 'a"b',
                    "a\\b", "a[b]", "x" * 65, " "):
            value = copy.deepcopy(GRAPH)
            value["nodes"][0]["label"] = bad
            cases.append(value)
        for bad in ("too_long_id", "A-1", None, 42):
            value = copy.deepcopy(GRAPH)
            value["nodes"][0]["id"] = bad
            cases.append(value)
        for graph in cases:
            with self.subTest(graph=graph), self.assertRaises(DiagramInputError):
                parse_diagram_json(json.dumps(graph))

    def test_compact_graph_and_bracketed_prose(self):
        compact = {"nodes": ["Exercise", "Heart health", "Endurance"],
                   "edges": [[1, 2], [2, 3, "supports"]]}
        raw = "Here is the graph [concepts and relationships]:\n" + json.dumps(compact) + "\n[End of graph]"
        canonical, graph = parse_diagram_json(raw)
        self.assertEqual(graph, GRAPH)
        self.assertEqual(parse_flowchart(canonical), (canonical, graph))
        self.assertEqual(parse_diagram_json(json.dumps({**compact, "direction": "LR"}))[1]["direction"], "LR")

    def test_redundant_nodes_and_edges_are_normalized(self):
        compact = {"nodes": ["Exercise", "Heart health", "Endurance", "Unused"],
                   "edges": [[1, 2], [1, 2], [2, 3, "supports"], [4, 4]]}
        self.assertEqual(parse_diagram_json(json.dumps(compact))[1], GRAPH)
        named = copy.deepcopy(GRAPH)
        named["nodes"] += [{"id": "A", "label": "Exercise"}, {"id": "D", "label": "Unused"}]
        named["edges"] += [{"from": "A", "to": "B"}, {"from": "D", "to": "D"}]
        self.assertEqual(parse_diagram_json(json.dumps(named))[1], GRAPH)

    def test_compact_graph_rejects_unknown_endpoints_and_ambiguous_content(self):
        base = {"nodes": ["A", "B", "C"], "edges": [[1, 2], [2, 3]]}
        cases = [
            ({**base, "edges": [[0, 2]]}, "unknown_endpoint"),
            ({**base, "edges": [[1, 4]]}, "unknown_endpoint"),
            ({**base, "edges": [[True, 2]]}, "invalid_edge_index"),
            ({**base, "edges": [[1.0, 2]]}, "invalid_edge_index"),
            ({**base, "edges": [[1, 2, "x", "y"]]}, "invalid_index_edge"),
            ({**base, "edges": [[1, 1]]}, "no_usable_edges"),
            ({**base, "edges": [[1, 2], [1, 2, "different"]]}, "conflicting_edge"),
            ({**base, "edges": [[1, 2], [2, 3], [3, 1]]}, "cyclic_graph"),
            ({"nodes": ["A", "B", "C", "D"], "edges": [[1, 2], [3, 4]]}, "disconnected_graph"),
            ({**base, "nodes": ["A", {"id": "B", "label": "B"}, "C"]}, "mixed_node_formats"),
            ({**base, "nodes": ["A", "<script>", "C"]}, "invalid_label"),
            ({**base, "nodes": ["A", "B", "<script>"], "edges": [[1, 2]]}, "invalid_label"),
            ({**GRAPH, "nodes": GRAPH["nodes"] + [{"id": "A", "label": "Other"}]}, "conflicting_node"),
            ({**GRAPH, "nodes": [{"id": "A", "text": "A"}, {"id": "B", "label": "B"}]}, "invalid_node_fields"),
        ]
        for graph, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(DiagramInputError, "^" + reason + "$"):
                parse_diagram_json(json.dumps(graph))

    def test_unicode_and_label_bounds(self):
        value = copy.deepcopy(GRAPH)
        value["direction"] = "LR"
        value["nodes"][0]["label"] = "現" * 64
        value["nodes"][1]["label"] = "المستقبل"
        value["edges"][1]["label"] = "関" * 24
        self.assertEqual(parse_diagram_json(json.dumps(value, ensure_ascii=False))[1], value)
        value["edges"][1]["label"] += "関"
        with self.assertRaises(DiagramInputError):
            parse_diagram_json(json.dumps(value))

    def test_reported_mermaid_failure_is_not_guessed_into_json(self):
        raw = 'Exercise --> Strengthen_Heart\nStrengthen_Heart --> Better_Oxygen_Delivery\nBetter_Oxygen_Delivery --> Improved_Endurance\nExercise --> Improve_Endurance["Indirect"]'
        with self.assertRaisesRegex(DiagramInputError, "^missing_object$"):
            parse_diagram_json(raw)
