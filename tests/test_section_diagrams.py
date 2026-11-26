import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from transcriber.diagram_json import parse_diagram_json
from transcriber.diagram_parser import parse_flowchart
from transcriber.provider_admission import ProviderAdmission
from transcriber.section_diagrams import DiagramCache, DiagramRequest, digest_text


EXAMPLE = 'flowchart TD\nA["Present"]\nB["Future"]\nC["Clarity"]\nA --> C\nB -->|"compare"| C'


class DiagramParserTests(unittest.TestCase):
    def test_canonical_graph_and_optional_fence(self):
        canonical, graph = parse_flowchart(EXAMPLE)
        self.assertEqual(canonical, EXAMPLE)
        self.assertEqual(graph["direction"], "TD")
        self.assertEqual(graph["nodes"][0], {"id": "A", "label": "Present"})
        self.assertEqual(graph["edges"][1], {"from": "B", "to": "C", "label": "compare"})
        self.assertEqual(parse_flowchart("\x60\x60\x60mermaid\n" + EXAMPLE + "\n\x60\x60\x60"), (canonical, graph))
        self.assertEqual(parse_flowchart(EXAMPLE.replace("TD", "LR"))[1]["direction"], "LR")

    def test_rejects_invalid_graphs_and_executable_or_unbounded_input(self):
        examples = [
            "", "graph TD", EXAMPLE + "\nclick A call evil()", EXAMPLE + "\nstyle A fill:red",
            EXAMPLE + "\n%%{init: {}}%%", EXAMPLE + "\nC --> A", EXAMPLE + "\nA --> C",
            EXAMPLE + "\nA --> A", EXAMPLE + "\nA --> Z", EXAMPLE + "\nD[\"Disconnected\"]",
            EXAMPLE.replace('B["Future"]', 'A["Duplicate"]'), EXAMPLE.replace("Present", "<img>"),
            EXAMPLE.replace("Present", "&lt;script&gt;"), EXAMPLE.replace("Present", "a\u202eb"),
            EXAMPLE.replace("Present", "a\tb"), EXAMPLE.replace("Present", "x" * 65),
            EXAMPLE.replace("compare", "x" * 25), EXAMPLE.replace("Present", " "),
            EXAMPLE.replace("A --> C", "A --> C --> B"),
            EXAMPLE.replace("A --> C", "A --> C;"), "x" * 8193,
            "flowchart TD\n" + "\n".join('N%d["node"]' % i for i in range(11)) + "\nN0 --> N1",
            "flowchart TD\n" + "\n".join('N%d["node"]' % i for i in range(7)) + "\n"
            + "\n".join("N%d --> N%d" % (i, j) for i in range(7) for j in range(i + 1, 7)),
            'flowchart TD\nA["One"]\nB["Two"]\nC["Three"]\nA --> B',
        ]
        for value in examples:
            with self.subTest(value=value[:100]), self.assertRaises(ValueError):
                parse_flowchart(value)

    def test_unicode_code_point_bound_and_round_trip(self):
        value = EXAMPLE.replace("Present", "現" * 64).replace("Future", "المستقبل")
        canonical, graph = parse_flowchart(value)
        self.assertEqual(parse_flowchart(canonical), (canonical, graph))
        self.assertEqual(len(graph["nodes"][0]["label"]), 64)
        with self.assertRaises(ValueError):
            parse_flowchart(value.replace("現" * 64, "現" * 65))

    def test_request_rejects_nonexact_fields_and_bad_identity(self):
        body = dict(source_id="home", item_id="book", audio_revision="a" * 64,
                    start_ms=0, end_ms=10000, transcript_digest="b" * 64,
                    summary_model="test-model", summary_prompt_version="section-v1", summary_digest="c" * 64)
        self.assertEqual(DiagramRequest.parse(body).response(state="deferred")["schema"], "fitbaus.section-diagram/v1")
        for extra in ({"prompt": "override"}, {"retry": True}, {"summary_digest": "bad"},
                      {"summary_prompt_version": "recap-v1"}, {"start_ms": True}, {"summary_model": []}):
            with self.assertRaises((ValueError, TypeError)):
                DiagramRequest.parse({**body, **extra})

    def test_recovery_is_confined_to_model_input_not_canonical_validation(self):
        raw = json.dumps({"nodes": ["Present", "Future", "Clarity"],
                          "edges": [[1, 3], [2, 3, "compare"], [3, 4]]})
        canonical, graph = parse_diagram_json(raw)
        self.assertEqual(canonical, EXAMPLE)
        with self.assertRaises(ValueError):
            parse_flowchart(canonical + "\nC --> D")
        with tempfile.TemporaryDirectory() as directory:
            key = digest_text("recovered")
            self.assertTrue(DiagramCache(directory).put(key, canonical))
            self.assertEqual(parse_flowchart(DiagramCache(directory).get(key)), (canonical, graph))

    def test_cache_reopen_and_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DiagramCache(Path(directory), max_entries=1)
            first, second = digest_text("first"), digest_text("second")
            self.assertTrue(cache.put(first, EXAMPLE))
            self.assertEqual(DiagramCache(directory).get(first), EXAMPLE)
            self.assertTrue(cache.put(second, EXAMPLE))
            self.assertIsNone(cache.get(first))
            self.assertEqual(cache.get(second), EXAMPLE)


class AdmissionTests(unittest.TestCase):
    def test_foreground_waiter_has_priority_over_new_diagrams(self):
        slot = ProviderAdmission(foreground_timeout=2)
        self.assertTrue(slot.acquire_diagram())
        self.assertFalse(slot.acquire_diagram())
        entered = threading.Event()
        release = threading.Event()

        def foreground():
            if slot.acquire_foreground():
                entered.set()
                release.wait(2)
                slot.release()

        worker = threading.Thread(target=foreground)
        worker.start()
        try:
            deadline = time.monotonic() + 1
            while slot.diagram_dispatch_allowed() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(slot.diagram_dispatch_allowed())
            self.assertFalse(slot.acquire_foreground())
            slot.release()
            self.assertTrue(entered.wait(1))
            self.assertFalse(slot.acquire_diagram())
            self.assertFalse(slot.acquire_foreground())
        finally:
            release.set()
            worker.join(3)
        self.assertTrue(slot.acquire_diagram())
        slot.release()

    def test_foreground_busy_and_wait_timeout(self):
        slot = ProviderAdmission(foreground_timeout=0.01)
        self.assertTrue(slot.acquire_foreground())
        self.assertFalse(slot.acquire_foreground())
        self.assertFalse(slot.acquire_diagram())
        slot.release()
        self.assertTrue(slot.acquire_diagram())
        self.assertFalse(slot.acquire_foreground())
        self.assertTrue(slot.diagram_dispatch_allowed())
        slot.release()
        self.assertTrue(slot.acquire_foreground())
        slot.release()
