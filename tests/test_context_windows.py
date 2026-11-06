from __future__ import annotations

import copy
import unittest

from subtitle_tool.subtitles import normalize, source_context
from tests.fixtures import result, timed_raw


BACKGROUND = 'Charges gather on the conductor surface.'
TARGET = 'It stays on the outside.'


def context_raw():
    """Synthetic background separated by transitions and a six-second pause."""
    raw = result((BACKGROUND, 'en'), ('Let us continue.', 'en'), ('Now look here.', 'en'),
                 ('As we said.', 'en'), (TARGET, 'en'), ('Keep that in mind.', 'en'))
    sentences = raw['transcripts'][0]['sentences']
    for sentence, (begin, end) in zip(sentences, [(0, 900), (6900, 7200), (7300, 7600),
                                                (7700, 8000), (8100, 8850), (9000, 9500)]):
        sentence.update(begin_time=begin, end_time=end)
    sentences[4] = timed_raw(TARGET.split(), word_ms=150, start=8100)['transcripts'][0]['sentences'][0]
    return raw


class ContextWindowTests(unittest.TestCase):
    def test_new_window_reaches_background_across_transitions_and_pause(self):
        sources = normalize(context_raw(), 0, '0000')[0]
        new = source_context(sources, 4)
        self.assertIn(BACKGROUND, [c['text'] for c in new])
        self.assertEqual(len(new), 5)

    def test_twelve_sentence_limit_in_both_directions(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(29)]),
                            0, '0000')[0]
        context = source_context(sources, 14)
        for side, expected in [('before', sources[2:14]), ('after', sources[15:27])]:
            self.assertEqual({c['text'] for c in context if c['position'] == side},
                             {s['text'] for s in expected})

    def test_thirty_seconds_uses_nearest_boundaries_inclusively(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000')[0]
        # Long outer sentences distinguish nearest edges from starts, ends or midpoints.
        for source, (begin, end) in zip(sources, [(0, 100000), (114999, 115000), (130000, 131000),
                                               (146000, 146001), (161000, 261000)]):
            source.update(begin_ms=begin, end_ms=end)
        for delta in [-1, 0, 1]:
            with self.subTest(distance=30000 + delta):
                changed = copy.deepcopy(sources)
                changed[0]['end_ms'] -= delta
                changed[4]['begin_ms'] += delta
                texts = [c['text'] for c in source_context(changed, 2)]
                for index in [0, 4]:
                    self.assertEqual(changed[index]['text'] in texts, delta <= 0)
                self.assertTrue(all(changed[i]['text'] in texts for i in [1, 3]))

    def test_fifteen_second_pause_is_inclusive_and_stops_each_path(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000')[0]
        sources[2].update(begin_ms=50000, end_ms=51000)
        for gap in [14999, 15000, 15001]:
            with self.subTest(gap=gap):
                sources[1].update(begin_ms=49000 - gap, end_ms=50000 - gap)
                sources[0].update(begin_ms=48000 - gap, end_ms=49000 - gap)
                sources[3].update(begin_ms=51000 + gap, end_ms=52000 + gap)
                sources[4].update(begin_ms=52000 + gap, end_ms=53000 + gap)
                context = source_context(sources, 2)
                self.assertEqual(len(context), 4 if gap <= 15000 else 0)

    def test_incompatible_neighbor_stops_direction_before_more_distant_matches(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000')[0]
        for index, side in [(1, 'after'), (3, 'before')]:
            for changes in [{'language': 'fr'}, {'source_lang': 'French'},
                            {'decision': 'original'}, {'decision_origin': 'manual'}]:
                with self.subTest(index=index, changes=changes):
                    changed = copy.deepcopy(sources)
                    changed[index].update(changes)
                    context = source_context(changed, 2)
                    self.assertEqual(len(context), 2)
                    self.assertTrue(all(c['position'] == side for c in context))

    def test_unknown_language_requires_matching_manual_explicit_language(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000')[0]
        for source in sources:
            source['language'] = ''
        self.assertEqual(source_context(sources, 2), [])
        for source in sources:
            source.update(decision_origin='manual', source_lang='English')
        self.assertEqual(len(source_context(sources, 2)), 4)
        sources[3]['source_lang'] = 'auto'
        self.assertTrue(all(c['position'] == 'before' for c in source_context(sources, 2)))

    def test_context_order_uses_time_then_sentence_distance_then_source_position(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000')[0]
        for source, (begin, end) in zip(sources, [(0, 9000), (9100, 9200), (10000, 11000),
                                               (11100, 11200), (11300, 11400)]):
            source.update(begin_ms=begin, end_ms=end)
        self.assertEqual([c['text'] for c in source_context(sources, 2)],
                         [sources[i]['text'] for i in [3, 4, 1, 0]])
        for source in sources:
            source.update(begin_ms=0, end_ms=1000)
        self.assertEqual([c['text'] for c in source_context(sources, 2)],
                         [sources[i]['text'] for i in [1, 3, 0, 4]])
