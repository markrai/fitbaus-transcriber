import json
import tempfile
import unittest

from transcriber.section_concepts import (
    CONCEPT_PROMPT, ConceptCache, ConceptInputError, ConceptRequest, MAX_CONCEPTS, MAX_CONCEPT_LENGTH,
    _cached_concepts, parse_concepts,
)
from transcriber.summary_cache import SummaryCacheError


class ConceptTests(unittest.TestCase):
    def test_plain_lines_bullets_numbering_and_duplicate_concepts(self):
        raw = "Here are the key concepts:\r\n- **Dopamine**\n2. Anticipation motivates action\n" \
              "• dopamine\n* Wanting versus liking\n\nExpected rewards reinforce habits"
        self.assertEqual(parse_concepts(raw), [
            "Dopamine", "Anticipation motivates action", "Wanting versus liking",
            "Expected rewards reinforce habits",
        ])

    def test_ordering_is_a_prompt_instruction_not_a_validation_requirement(self):
        self.assertIn("List the ideas in the order they appear in the summary.", CONCEPT_PROMPT)
        values = ["Third idea", "First idea", "Second idea"]
        self.assertEqual(parse_concepts("\n".join(values)), values)
        self.assertEqual(_cached_concepts(json.dumps(values)), values)

    def test_fenced_unicode_and_disjointed_ideas(self):
        raw = "```text\n# Main ideas\n- 未来の選択\n- الرغبة والمتعة\n- Cafe\u0301\n- Café\n```"
        self.assertEqual(parse_concepts(raw), ["未来の選択", "الرغبة والمتعة", "Cafe\u0301"])
        self.assertEqual(parse_concepts("A single standalone idea"), ["A single standalone idea"])

    def test_bounded_output_and_invalid_lines_do_not_erase_valid_concepts(self):
        raw = "\n".join(["<script>unsafe</script>", "a\u202eb", "x" * (MAX_CONCEPT_LENGTH + 1),
                         "---", "A useful idea"] + ["Concept " + str(i) for i in range(20)])
        concepts = parse_concepts(raw)
        self.assertEqual(len(concepts), MAX_CONCEPTS)
        self.assertEqual(concepts[0], "A useful idea")
        self.assertEqual(concepts[-1], "Concept 8")
        self.assertEqual(parse_concepts("現" * MAX_CONCEPT_LENGTH), ["現" * MAX_CONCEPT_LENGTH])

    def test_unusable_or_unbounded_inputs_fail(self):
        for raw, reason in (
            (None, "invalid_input"), ("x" * 8193, "input_too_large"),
            ("\ud800", "invalid_unicode"), ("Concepts:\n---", "no_usable_concepts"),
            ("<script>bad</script>\n&lt;", "no_usable_concepts"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(ConceptInputError, "^" + reason + "$"):
                parse_concepts(raw)

    def test_cached_concepts_are_strictly_revalidated(self):
        valid = ["One", "Two"]
        self.assertEqual(_cached_concepts(json.dumps(valid)), valid)
        for value in ([], ["One", "one"], ["<img>"], [" x "], ["x" * 141],
                      ["---"], {"concepts": valid}, [1], ["\ud800"], ["a"] * 11):
            with self.subTest(value=value), self.assertRaises(SummaryCacheError):
                _cached_concepts(json.dumps(value))

    def test_separate_cache_persists_and_evicts(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ConceptCache(directory, max_entries=1)
            self.assertEqual(cache.path.name, "concepts.sqlite3")
            self.assertTrue(cache.put("a" * 64, '["One"]'))
            self.assertEqual(_cached_concepts(ConceptCache(directory).get("a" * 64)), ["One"])
            self.assertTrue(cache.put("b" * 64, '["Two"]'))
            self.assertIsNone(cache.get("a" * 64))

    def test_request_identity_and_versions(self):
        body = dict(source_id="home", item_id="book", audio_revision="a" * 64, start_ms=0,
                    end_ms=10000, transcript_digest="b" * 64, summary_model="test-model",
                    summary_prompt_version="section-v1", summary_digest="c" * 64)
        response = ConceptRequest.parse(body).response(state="deferred", retry_after_seconds=3)
        self.assertEqual(response["schema"], "fitbaus.section-concepts/v1")
        self.assertEqual(response["concept_prompt_version"], "concepts-v2")
        self.assertEqual(response["concept_input_version"], "plain-lines-v1")
        for key, value in body.items():
            self.assertEqual(response[key], value)
        with self.assertRaises(ValueError):
            ConceptRequest.parse({**body, "prompt": "override"})
