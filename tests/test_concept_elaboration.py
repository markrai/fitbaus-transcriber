import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from transcriber.section_concepts import (
    ELABORATION_PROMPT, ConceptRequest, ElaborationRequest, generate_elaboration,
)
from transcriber.summaries import SummaryError


class ConceptElaborationTest(unittest.TestCase):
    def setUp(self):
        self.parent = ConceptRequest.parse({
            "source_id": "home", "item_id": "book", "audio_revision": "a" * 64,
            "start_ms": 0, "end_ms": 300000, "transcript_digest": "b" * 64,
            "summary_model": "test-model", "summary_prompt_version": "section-v1",
            "summary_digest": "c" * 64,
        })
        self.request = ElaborationRequest(self.parent, 1, "Integral calculus")
        self.provider = Mock()
        self.provider.complete.return_value = "An explanation.\n\nAn example."
        self.cache = Mock()
        self.cache.get.return_value = '["Differentiation","Integral calculus"]'
        self.options = dict(summary_cache=Mock(), concept_cache=self.cache,
                            source_url="http://source", provider_current=lambda: True,
                            dispatch_allowed=lambda: True)

    def generate(self, request=None):
        return generate_elaboration(request or self.request, "token", Mock(), self.provider,
                                    SimpleNamespace(model="test-model"), lambda: True, **self.options)

    @patch("transcriber.section_concepts.resolve_parent", return_value=("key", "private summary"))
    def test_only_selected_card_is_sent_to_provider(self, resolve):
        response = self.generate()
        self.provider.complete.assert_called_once_with(ELABORATION_PROMPT, "Integral calculus")
        self.assertEqual(response["explanation"], "An explanation.\n\nAn example.")
        self.assertEqual(response["index"], 1)
        self.assertEqual(resolve.call_count, 2)
        self.cache.put.assert_not_called()

    @patch("transcriber.section_concepts.resolve_parent", return_value=("key", "private summary"))
    def test_rejects_forged_or_replaced_card(self, resolve):
        with self.assertRaises(SummaryError):
            self.generate(ElaborationRequest(self.parent, 1, "Different concept"))
        self.provider.complete.assert_not_called()
        self.cache.get.side_effect = ['["Differentiation","Integral calculus"]', '["Different concept"]']
        with self.assertRaises(SummaryError):
            self.generate()

    @patch("transcriber.section_concepts.resolve_parent", return_value=("key", "private summary"))
    def test_deferred_admission_does_not_infer(self, resolve):
        self.options["dispatch_allowed"] = lambda: False
        self.assertEqual(self.generate()["state"], "deferred")
        self.provider.complete.assert_not_called()

    @patch("transcriber.section_concepts.resolve_parent", return_value=("key", "private summary"))
    def test_invalid_completion_is_rejected(self, resolve):
        for text in ("", "x" * 8001, "bad\x00text"):
            self.provider.complete.return_value = text
            with self.assertRaises(SummaryError):
                self.generate()

    def test_request_rejects_invalid_index(self):
        parent = self.parent.response()
        parent = {key: value for key, value in parent.items() if key in {
            "source_id", "item_id", "audio_revision", "start_ms", "end_ms",
            "transcript_digest", "summary_model", "summary_prompt_version", "summary_digest"}}
        self.assertEqual(ElaborationRequest.parse({
            "parent": parent, "index": 1, "concept": "Integral calculus"}).index, 1)
        for index in (True, -1, 10, "1"):
            with self.assertRaises(ValueError):
                ElaborationRequest.parse({"parent": parent, "index": index, "concept": "Integral calculus"})
