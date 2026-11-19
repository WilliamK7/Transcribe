from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from subtitle_tool.common import ASR_MODEL, ASR_LANGUAGE_INFERENCE_VERSION, ToolError
from subtitle_tool.engine import Engine
from subtitle_tool.policy import load_policy
from subtitle_tool.review import ReviewDecision
from subtitle_tool.subtitles import make_fragment, normalize
from tests.fixtures import FakeService, SavedResultFixture, result, unlabeled_result


class LanguageRuleTests(unittest.TestCase):
    def normalize(self, *texts):
        return normalize(unlabeled_result(*texts), 0, '0000', asr_model=ASR_MODEL)[0]

    def test_latin_scripts_translate_without_inventing_an_asr_or_mt_language(self):
        for text in ['Hello world.', 'Bonjour à tous.', '¿Cómo está usted?', 'Écoutez.',
                     'Это пример.', 'HELLO WORLD.', 'This is a complete sentence.']:
            with self.subTest(text=text):
                source = self.normalize(text)[0]
                self.assertEqual((source['decision'], source['language'], source['source_lang']),
                                 ('translate', '', 'auto'))
                self.assertEqual(source['decision_origin'], 'automatic')

    def test_chinese_brands_and_substantive_mixed_speech(self):
        for text in ['欢迎使用 Python 和 API。', '欢迎使用Python和API。', '我们使用 Windows。', '谢谢。', '好的。']:
            with self.subTest(text=text):
                self.assertEqual(self.normalize(text)[0]['decision'], 'original')
        for text in ['他说 please turn on the light now。', '他说please turn on the light。']:
            with self.subTest(text=text):
                self.assertEqual(self.normalize(text)[0]['decision'], 'translate')

    def test_isolated_ambiguous_tokens_do_not_default_to_english_or_chinese(self):
        for text in ['OK', 'Hmm', '東京都港区', 'API', 'Python', 'X']:
            with self.subTest(text=text):
                self.assertEqual(self.normalize(text)[0]['decision'], 'uncertain')

    def test_short_latin_cues_use_nearby_independent_evidence(self):
        sources = self.normalize('We can continue with the example.', 'OK.', 'Hmm',
                                 'We can solve this problem.')
        self.assertEqual([s['decision'] for s in sources], ['translate'] * 4)
        self.assertTrue(all(s['language'] == '' and s['source_lang'] == 'auto' for s in sources))

    def test_context_does_not_cross_language_switch_or_long_pause(self):
        sources = self.normalize('We can solve this problem.', '欢迎使用本课程。', 'OK')
        self.assertEqual(sources[-1]['decision'], 'uncertain')
        raw = unlabeled_result('We can solve this problem.', 'OK')
        raw['transcripts'][0]['sentences'][1].update(begin_time=16001, end_time=16901)
        self.assertEqual(normalize(raw, 0, 's', asr_model=ASR_MODEL)[0][-1]['decision'], 'uncertain')
        sources = self.normalize('We can solve this problem.', 'OK', 'Hmm', 'OK', 'Hmm')
        self.assertEqual(sources[-1]['decision'], 'uncertain')

    def test_short_han_text_only_uses_chinese_context(self):
        self.assertEqual(self.normalize('欢迎来到本课程。', '复数')[1]['decision'], 'original')
        sources = self.normalize('We can solve this problem.', '東京都港区')
        self.assertEqual(sources[1]['decision'], 'uncertain')

    def test_explicit_conflicting_and_unsupported_asr_labels_still_require_review(self):
        for text, language in [('This is an English sentence.', 'zh'), ('欢迎来到本课程。', 'en'),
                               ('This is a complete sentence.', 'xx')]:
            with self.subTest(language=language):
                source = normalize(result((text, language)), 0, 's', asr_model=ASR_MODEL)[0][0]
                self.assertEqual(source['decision'], 'uncertain')

    def test_original_words_text_and_timing_are_unchanged(self):
        from tests.fixtures import unlabeled_raw
        raw = unlabeled_raw(['Hello', 'world.'], language='en')
        before = copy.deepcopy(raw)
        source = normalize(raw, 456, 's', asr_model=ASR_MODEL)[0][0]
        self.assertEqual(raw, before)
        self.assertEqual((source['begin_ms'], source['end_ms']), (456, 1456))
        self.assertEqual(len(source['words']), 2)


class LanguageRuleRecoveryTests(SavedResultFixture):
    def save_legacy_unlabeled_result(self):
        raw = unlabeled_result('Keep this sentence unchanged.', 'We can solve this problem.', 'OK.')
        self.save_result(raw)
        manifest = self.store.job(self.job_id)['manifest']
        manifest['contract'].pop('language_inference', None)
        sources = normalize(raw, 0, '0000', asr_model=ASR_MODEL)[0]
        for source in sources:
            source['decision'] = 'uncertain'
        self.store.initialize_subtitles(self.job_id, sources, [make_fragment(s) for s in sources], manifest)
        self.store.update(self.job_id, 'needs_review', code='language_uncertain')
        self.store.apply_review(self.job_id, sources[0]['cue_id'], ReviewDecision('original'))
        return raw

    def test_retired_language_rules_rebuild_from_raw_and_preserve_manual_decisions(self):
        self.save_legacy_unlabeled_result()
        raw_path = self.cache / 'raw-0000.json'
        before_raw = raw_path.read_bytes()
        before_sources = self.store.sources(self.job_id)
        engine = Engine(self.config, self.store)
        with self.assertRaises(ToolError) as caught:
            engine.resume(self.job_id)
        self.assertEqual(caught.exception.code, 'cache_version')
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'failed')
        self.assertEqual(self.store.sources(self.job_id), before_sources)
        self.assertEqual(fake.calls, [])
        with mock.patch('requests.request', side_effect=AssertionError('No network during reflow')):
            engine.reflow(self.job_id)
        sources = self.store.sources(self.job_id)
        self.assertEqual([s['decision'] for s in sources], ['original', 'translate', 'translate'])
        self.assertEqual(sources[0], before_sources[0])
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['contract']['language_inference'], ASR_LANGUAGE_INFERENCE_VERSION)
        snapshot = json.loads((self.path / manifest['reflow']['snapshot']).read_bytes())
        self.assertEqual(snapshot['sources'], before_sources)
        self.assertEqual(raw_path.read_bytes(), before_raw)
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertTrue(fake.calls)
        self.assertTrue(all(call[1].endswith('/chat/completions') for call in fake.calls))
        self.assertTrue(all(s['source_lang'] == 'auto' and s['language'] == '' for s in sources))
        self.assertEqual(raw_path.read_bytes(), before_raw)

    def test_new_raw_result_proceeds_to_translation_without_review(self):
        self.save_result(unlabeled_result('This is a complete sentence.', 'OK.'), normalized=False)
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertTrue(fake.calls)
        self.assertTrue(all(call[1].endswith('/chat/completions') for call in fake.calls))
        for call in fake.calls:
            data = json.loads(call[2]['json']['messages'][1]['content'])
            self.assertTrue(all(s['asr_language'] == '' and s['source_language'] == 'auto'
                                for s in data['sentences']))

    def test_invalid_language_rule_revision_fails_without_network(self):
        self.save_result()
        original = self.store.job(self.job_id)['manifest']
        for value in [True, 1.0, '1', 999, None]:
            with self.subTest(value=value):
                manifest = copy.deepcopy(original)
                manifest['contract']['language_inference'] = value
                with self.assertRaises(ToolError) as caught:
                    load_policy(manifest)
                self.assertEqual(caught.exception.code, 'cache_version')
