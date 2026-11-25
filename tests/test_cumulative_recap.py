import unittest

from transcriber.contract import AudioIdentity, Cue, TranscriptWindow
from transcriber.cumulative_recap import CumulativeRecapRequest, Node, _leaf, _tree, MAX_POSITION_MS


IDENTITY = AudioIdentity("home", "book", "a" * 64)


class CumulativeRecapTests(unittest.TestCase):
    def test_maximum_tree_is_bounded_and_contiguous(self):
        leaves = [Node(start, start + 600000, str(start), "text")
                  for start in range(0, MAX_POSITION_MS, 600000)]
        root = _tree(leaves)
        self.assertEqual((root.start, root.end), (0, MAX_POSITION_MS))
        self.assertLessEqual(root.count, 192)

        def validate(node):
            if node.children:
                self.assertTrue(2 <= len(node.children) <= 4)
                self.assertEqual(node.start, node.children[0].start)
                self.assertEqual(node.end, node.children[-1].end)
                self.assertTrue(all(a.end == b.start for a, b in zip(node.children, node.children[1:])))
                for child in node.children:
                    validate(child)
        validate(root)

    def test_leaf_digest_excludes_future_cues_but_tracks_profile(self):
        def make(future, profile="small"):
            window = TranscriptWindow(IDENTITY, 0, 300000,
                                      (Cue(1000, 2000, "heard"), Cue(20000, 21000, future)))
            return _leaf(0, 10000, [(profile, window)])
        first = make("one")
        self.assertEqual(first.text, "heard")
        self.assertEqual(first.digest, make("two").digest)
        self.assertNotEqual(first.digest, make("two", "base").digest)

    def test_tree_keeps_completed_groups_stable_when_prefix_advances(self):
        leaves = [Node(i * 600000, (i + 1) * 600000, str(i), "text") for i in range(5)]
        previous = _tree(leaves[:4])
        extended = _tree(leaves)
        self.assertEqual(extended.children[0], previous)
        self.assertEqual(extended.children[1], leaves[4])
        self.assertNotEqual(extended.digest, previous.digest)
        self.assertIsNone(_tree([]))

    def test_position_bounds(self):
        self.assertEqual(CumulativeRecapRequest(IDENTITY, MAX_POSITION_MS).end_ms, MAX_POSITION_MS)
        for value in (True, -1, MAX_POSITION_MS + 1, 1.5):
            with self.assertRaises(ValueError):
                CumulativeRecapRequest(IDENTITY, value)
