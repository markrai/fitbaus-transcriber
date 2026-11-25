import unittest

from transcriber.contract import AudioIdentity, Cue
from transcriber.section_plan import SectionPlanRequest, choose_cut


IDENTITY = AudioIdentity("home", "book", "a" * 64)


class SectionPlanTests(unittest.TestCase):
    def test_sentence_and_pause_rank_before_distance(self):
        cues = [
            Cue(280000, 285000, 'A sentence.”'),
            Cue(289000, 299000, "Nearer."),
            Cue(299100, 300500, "unfinished"),
            Cue(301000, 310000, "continuing"),
        ]
        self.assertEqual(choose_cut(300000, cues), 285000)

    def test_sentence_or_pause_and_tie_breaking(self):
        self.assertEqual(choose_cut(300000, [Cue(290000, 299000, "phrase"),
                                            Cue(301000, 305000, "next")]), 299000)
        self.assertEqual(choose_cut(300000, [Cue(290000, 299000, "One."),
                                            Cue(299000, 301000, "Two.")]), 299000)
        self.assertEqual(choose_cut(300000, [Cue(280000, 285000, "No punctuation")]), 300000)
        self.assertEqual(choose_cut(300000, [Cue(260000, 269999, "Too far.")]), 300000)

    def test_fixed_edges_and_conservative_slack(self):
        request = SectionPlanRequest(IDENTITY, (0, 480000, 960000, 1440000, 1920000))
        self.assertEqual(request.movable(), (1, 2, 3))
        for signs in ((-1, 1, -1), (1, -1, 1)):
            moved = [0] + [request.boundaries[i] + signs[i-1] * 30000 for i in (1, 2, 3)] + [1920000]
            self.assertTrue(all(0 < b - a <= 600000 for a, b in zip(moved, moved[1:])))
        self.assertEqual(SectionPlanRequest(IDENTITY, (0, 600000, 900000)).movable(), ())
        self.assertEqual(SectionPlanRequest(IDENTITY, (0, 60000, 360000)).movable(), ())
        self.assertEqual(SectionPlanRequest(IDENTITY, (0, 300000)).movable(), ())

    def test_window_math_at_boundary_and_book_end(self):
        request = SectionPlanRequest(IDENTITY, (0, 300000, 360001))
        self.assertEqual(request.required_windows(360001), [(0, 300000), (300000, 360001)])
        request = SectionPlanRequest(IDENTITY, (0, 480000, 960000))
        self.assertEqual(request.required_windows(960000), [(300000, 600000)])

    def test_rejects_invalid_partitions(self):
        for cuts in ((0,), (0, True), (0, 600001), (10, 9), (0, 1, 2, 3, 4, 5), (-1, 10)):
            with self.subTest(cuts=cuts), self.assertRaises(ValueError):
                SectionPlanRequest(IDENTITY, cuts)
