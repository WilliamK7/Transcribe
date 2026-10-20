from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from subtitle_tool.common import MT_INPUT_BUDGET, MT_TEMPLATE_RESERVE
from subtitle_tool.engine import Engine
from subtitle_tool.mt import batch_input_size
from subtitle_tool.policy import current_execution_snapshot, load_policy
from subtitle_tool.subtitles import (make_fragment, normalize, plan_reference_units, plan_translation_batches,
                                     source_context, translation_context)
from tests.fixtures import FakeService, SavedResultFixture, result, timed_raw
from tests.test_translation_v3 import batch_input, batch_response


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
        sources = normalize(context_raw(), 0, '0000', preserve_sentences=True)[0]
        old = source_context(sources, 4, context_version=0)
        new = source_context(sources, 4, context_version=1)
        self.assertNotIn(BACKGROUND, [c['text'] for c in old])
        self.assertIn(BACKGROUND, [c['text'] for c in new])
        self.assertEqual(len(old), 3)
        self.assertEqual(len(new), 5)

    def test_twelve_sentence_limit_in_both_directions(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(29)]),
                            0, '0000', preserve_sentences=True)[0]
        context = source_context(sources, 14, context_version=1)
        for side, expected in [('before', sources[2:14]), ('after', sources[15:27])]:
            self.assertEqual({c['text'] for c in context if c['position'] == side},
                             {s['text'] for s in expected})
        self.assertEqual(len(source_context(sources, 14, context_version=0)), 4)

    def test_thirty_seconds_uses_nearest_boundaries_inclusively(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000', preserve_sentences=True)[0]
        # Long outer sentences distinguish nearest edges from starts, ends or midpoints.
        for source, (begin, end) in zip(sources, [(0, 100000), (114999, 115000), (130000, 131000),
                                               (146000, 146001), (161000, 261000)]):
            source.update(begin_ms=begin, end_ms=end)
        for delta in [-1, 0, 1]:
            with self.subTest(distance=30000 + delta):
                changed = copy.deepcopy(sources)
                changed[0]['end_ms'] -= delta
                changed[4]['begin_ms'] += delta
                texts = [c['text'] for c in source_context(changed, 2, context_version=1)]
                for index in [0, 4]:
                    self.assertEqual(changed[index]['text'] in texts, delta <= 0)
                self.assertTrue(all(changed[i]['text'] in texts for i in [1, 3]))

    def test_fifteen_second_pause_is_inclusive_and_stops_each_path(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000', preserve_sentences=True)[0]
        sources[2].update(begin_ms=50000, end_ms=51000)
        for gap in [14999, 15000, 15001]:
            with self.subTest(gap=gap):
                sources[1].update(begin_ms=49000 - gap, end_ms=50000 - gap)
                sources[0].update(begin_ms=48000 - gap, end_ms=49000 - gap)
                sources[3].update(begin_ms=51000 + gap, end_ms=52000 + gap)
                sources[4].update(begin_ms=52000 + gap, end_ms=53000 + gap)
                context = source_context(sources, 2, context_version=1)
                self.assertEqual(len(context), 4 if gap <= 15000 else 0)

    def test_incompatible_neighbor_stops_direction_before_more_distant_matches(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000', preserve_sentences=True)[0]
        for index, side in [(1, 'after'), (3, 'before')]:
            for changes in [{'language': 'fr'}, {'source_lang': 'French'},
                            {'decision': 'original'}, {'decision_origin': 'manual'}]:
                with self.subTest(index=index, changes=changes):
                    changed = copy.deepcopy(sources)
                    changed[index].update(changes)
                    context = source_context(changed, 2, context_version=1)
                    self.assertEqual(len(context), 2)
                    self.assertTrue(all(c['position'] == side for c in context))

    def test_unknown_language_requires_matching_manual_explicit_language(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000', preserve_sentences=True)[0]
        for source in sources:
            source['language'] = ''
        for version in [0, 1]:
            with self.subTest(version=version):
                self.assertEqual(source_context(sources, 2, context_version=version), [])
        for source in sources:
            source.update(decision_origin='manual', source_lang='English')
        self.assertEqual(len(source_context(sources, 2, context_version=1)), 4)
        sources[3]['source_lang'] = 'auto'
        self.assertTrue(all(c['position'] == 'before' for c in source_context(sources, 2, context_version=1)))

    def test_context_order_uses_time_then_sentence_distance_then_source_position(self):
        sources = normalize(result(*[(f'Sentence {i}.', 'en') for i in range(5)]),
                            0, '0000', preserve_sentences=True)[0]
        for source, (begin, end) in zip(sources, [(0, 9000), (9100, 9200), (10000, 11000),
                                               (11100, 11200), (11300, 11400)]):
            source.update(begin_ms=begin, end_ms=end)
        self.assertEqual([c['text'] for c in source_context(sources, 2, context_version=1)],
                         [sources[i]['text'] for i in [3, 4, 1, 0]])
        for source in sources:
            source.update(begin_ms=0, end_ms=1000)
        self.assertEqual([c['text'] for c in source_context(sources, 2, context_version=1)],
                         [sources[i]['text'] for i in [1, 3, 0, 4]])

    def test_expanded_context_is_trimmed_from_tail_as_whole_items_with_repair_budget(self):
        sources = normalize(result(*[(f'Sentence {i}. ' + 'background ' * 40, 'en') for i in range(25)]),
                            0, '0000', preserve_sentences=True)[0]
        source = sources[12]
        neighbors = source_context(sources, 12, context_version=1)
        groups = plan_translation_batches([make_fragment(source)], neighbors)
        self.assertEqual(len(groups), 1)
        members, context = groups[0]
        self.assertGreater(len(context), 0)
        self.assertLess(len(context), len(neighbors))
        self.assertEqual(context, neighbors[:len(context)])
        texts = [c['text'] for c in members]
        self.assertLessEqual(batch_input_size(texts, source['source_lang'], context), MT_INPUT_BUDGET)
        self.assertGreater(batch_input_size(texts, source['source_lang'], neighbors[:len(context) + 1]),
                           MT_INPUT_BUDGET)

    def test_strategies_one_and_two_keep_legacy_reference_window(self):
        sources = normalize(context_raw(), 0, '0000', preserve_sentences=True)[0]
        for source in sources:
            source['reference'] = '模拟参考译文'
        cue = make_fragment(sources[4])
        context = translation_context(cue, sources, position=4)
        self.assertEqual(len(context), 3)
        self.assertNotIn(BACKGROUND, [c['source'] for c in context])


class ContextExecutionTests(SavedResultFixture):
    def run_context(self, *, split=False, recover=False):
        sources = self.save_result(context_raw())
        expected_ranges = None
        if split:
            parts = [make_fragment(sources[4], 0, 2), make_fragment(sources[4], 2, 5)]
            expected_ranges = [(c['cue_id'], c['begin_ms'], c['end_ms'], c['text']) for c in parts]
            cues = [c for s in sources for c in (parts if s == sources[4] else [make_fragment(s)])]
            manifest = self.store.job(self.job_id)['manifest']
            manifest.update(references_planned=True, layout_planned=True, layout_round=0, layout_complete=True)
            self.store.save_layout(self.job_id, cues, manifest,
                                   references=plan_reference_units(sources, strategy_version=3))
        fake = FakeService(context_raw())
        original = fake.request
        target_requests = []

        def handler(method, url, **kwargs):
            if not url.endswith('/chat/completions'):
                return original(method, url, **kwargs)
            payload = kwargs['json']
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) + MT_TEMPLATE_RESERVE,
                                 MT_INPUT_BUDGET)
            texts = [c['text'] for c in batch_input(payload)]
            if texts == [TARGET] or (split and all(t in [c['text'] for c in parts] for t in texts)):
                target_requests.append(payload['messages'][0]['content'].split('\n\n', 1)[0])
            return batch_response([] if recover and len(texts) > 1 else ['模拟译文'] * len(texts))

        fake.request = handler
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertTrue(all(BACKGROUND in c for c in target_requests))
        if split:
            actual = [(c['cue_id'], c['begin_ms'], c['end_ms'], c['text']) for c in self.store.cues(self.job_id)
                      if c['sentence_id'] == sources[4]['sentence_id']]
            self.assertEqual(actual, expected_ranges)
        return target_requests

    def test_new_job_reuses_whole_reference_with_expanded_background(self):
        self.assertEqual(current_execution_snapshot()['context_version'], 1)
        self.assertEqual(load_policy(self.store.job(self.job_id)['manifest']).context_version, 1)
        self.assertEqual(len(self.run_context()), 1)
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts['mt_reference_reused'], 6)
        self.assertEqual(counts.get('mt_subtitle_request', 0), 0)

    def test_reference_and_subtitle_translation_use_same_expanded_window(self):
        requests = self.run_context(split=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])

    def test_split_retry_keeps_background_and_subtitle_ranges(self):
        self.assertEqual(len(self.run_context(split=True, recover=True)), 5)
        self.assertEqual(self.store.counts(self.job_id)['mt_split'], 1)

    def resume_legacy(self, snapshot):
        self.save_result(context_raw())
        manifest = self.store.job(self.job_id)['manifest']
        if snapshot is None:
            manifest.pop('execution_snapshot')
        else:
            manifest['execution_snapshot'] = snapshot
        self.store.update(self.job_id, manifest=manifest)
        self.assertEqual(load_policy(manifest).context_version, 0)
        fake = FakeService(context_raw())
        self.assertEqual(self.run_fake(fake), 'completed')
        saved = self.store.job(self.job_id)['manifest']
        self.assertEqual(saved.get('execution_snapshot'), snapshot)
        requests = [call[2]['json'] for call in fake.named('/chat/completions')
                    if [c['text'] for c in batch_input(call[2]['json'])] == [TARGET]]
        self.assertEqual(len(requests), 1)
        self.assertNotIn(BACKGROUND, requests[0]['messages'][0]['content'])
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(fake.uploads, [])
        return fake

    def test_snapshot_without_context_version_resumes_old_window_without_backfill(self):
        snapshot = current_execution_snapshot()
        snapshot.pop('context_version')
        self.resume_legacy(snapshot)

    def test_explicit_zero_context_version_resumes_old_window(self):
        self.resume_legacy({**current_execution_snapshot(), 'context_version': 0})

    def test_task_without_snapshot_resumes_old_window_without_backfill(self):
        self.resume_legacy(None)

    def test_reflow_switches_old_window_and_changed_request_misses_cache(self):
        snapshot = current_execution_snapshot()
        snapshot.pop('context_version')
        fake = self.resume_legacy(snapshot)
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.store.job(self.job_id)['manifest']['execution_snapshot']['context_version'], 1)
        fake.calls.clear()
        self.assertEqual(self.run_fake(fake), 'completed')
        target = [call[2]['json'] for call in fake.named('/chat/completions')
                  if [c['text'] for c in batch_input(call[2]['json'])] == [TARGET]]
        self.assertEqual(len(target), 1)
        self.assertIn(BACKGROUND, target[0]['messages'][0]['content'])
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(fake.uploads, [])

    def test_reflow_reuses_cache_when_actual_window_stays_identical(self):
        self.save_result()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot'].pop('context_version', None)
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed')
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        with mock.patch('requests.request', side_effect=AssertionError('identical request should reuse cache')):
            self.assertEqual(Engine(self.config, self.store).run(self.job_id), 'completed')
        self.assertEqual(self.store.counts(self.job_id)['mt_reference_cache_hit'], 1)
