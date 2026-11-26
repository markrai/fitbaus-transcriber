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
            (encoded + "\n" + encoded, "ambiguous_envelope"),
            ("[" + encoded + "]", "ambiguous_envelope"),
            ("{} " + encoded, "ambiguous_envelope"),
            (encoded + " {", "ambiguous_envelope"),
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
            {**GRAPH, "nodes": GRAPH["nodes"] + [{"id": "D", "label": "Unconnected"}]},
            {**GRAPH, "nodes": GRAPH["nodes"] + [{"id": "A", "label": "Duplicate"}]},
            {**GRAPH, "edges": GRAPH["edges"] + [{"from": "C", "to": "A"}]},
            {**GRAPH, "edges": GRAPH["edges"] + [{"from": "C", "to": "C"}]},
            {**GRAPH, "edges": GRAPH["edges"] + [{"from": "A", "to": "B"}]},
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
