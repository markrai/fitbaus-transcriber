import io
import json
import time
import unittest

from transcriber.summary_provider import SummaryProviderError, read_lobo_elaboration


def event(name, value):
    return ("event: " + name + "\ndata: " + json.dumps(value) + "\n\n").encode("utf-8")


class ElaborationStreamTest(unittest.TestCase):
    def test_text_is_delivered_before_terminal(self):
        first = event("text", {"delta": "Integral"})
        second = event("text", {"delta": " calculus"})
        terminal = event("done", {"finishReason": 0})
        stream = io.BytesIO(first + second + terminal)
        observed = []
        result = read_lobo_elaboration(stream, lambda text: observed.append((text, stream.tell())),
                                       time.monotonic() + 10)
        self.assertEqual(result, "Integral calculus")
        self.assertEqual(observed, [("Integral", len(first)), (" calculus", len(first + second))])

    def test_incomplete_stream_does_not_become_ready(self):
        first = event("text", {"delta": "Partial"})
        for tail in (b"", event("done", {"finishReason": 1}), event("error", {"error": "failure"})):
            observed = []
            with self.assertRaises(SummaryProviderError):
                read_lobo_elaboration(io.BytesIO(first + tail), observed.append, time.monotonic() + 10)
            self.assertEqual(observed, ["Partial"])

    def test_rejects_oversized_delta_before_publication(self):
        observed = []
        with self.assertRaises(SummaryProviderError):
            read_lobo_elaboration(io.BytesIO(event("text", {"delta": "x" * 8001})), observed.append,
                                  time.monotonic() + 10)
        self.assertEqual(observed, [])
