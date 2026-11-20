"""Untranslated prose must not pass just because the JSON is complete."""
import json
import unittest
import warnings

from subtitle_tool.engine import Engine
from tests.fixtures import result
from tests.mt_fixtures import PoolPipelineFixture


class UntranslatedOutputTests(unittest.TestCase):
    def test_copied_english_prose_is_rejected_even_after_cosmetic_changes(self):
        from subtitle_tool.mt_quality import BatchValidationError, validate_translation_content
        for source, target in [('The cache stores a previous result.', 'The cache stores a previous result.'),
                               ('And these values are just one.', 'AND THESE VALUES ARE JUST ONE！'),
                               ('This is bounded by five.', 'This is bounded by five。')]:
            with self.subTest(source=source), self.assertRaises(BatchValidationError) as caught:
                validate_translation_content({'0': target}, [source])
            self.assertEqual(caught.exception.category, 'translation_untranslated')
            self.assertNotIn(source, str(caught.exception))

    def test_identifiers_names_formulae_and_chinese_are_not_failed_for_matching(self):
        from subtitle_tool.mt_quality import validate_translation_content
        for source in ['S0, s1, s2, s3.', 'I 1.', 'cosθ + i.', 'x + y = z', 'OpenAI',
                       'The Lord of the Rings', 'University of New York', 'C minus', 'e to the i r',
                       '我们继续。', '请按 Enter 继续。']:
            with self.subTest(source=source):
                validate_translation_content({'0': source}, [source])
        validate_translation_content({'0': '这个值是 5。'}, ['This value is five.'])

    def test_code_fragments_are_not_rejected_for_preserving_their_syntax(self):
        from subtitle_tool.mt_quality import validate_translation_content
        for source in ['for key in data:', 'for i in range(10):', 'if value is not None:',
                       'while value is not None:', '  for key in data:  ', '`for key in data:`',
                       'values = [key for key in data]', 'return value if enabled else fallback',
                       'all(key in data for key in keys)', '[key for key in data]']:
            with self.subTest(source=source):
                validate_translation_content({'0': source}, [source])

    def test_prose_with_code_mentions_or_punctuation_still_requires_translation(self):
        from subtitle_tool.mt_quality import BatchValidationError, validate_translation_content
        for source in ['if the cache is empty:', 'For this example, the cache stores a result:',
                       'In Python, use for key in data:', 'This is not cached', 'return this or that',
                       'The value of x is (one).', 'The value of x = 1.',
                       '`The cache stores a previous result.`', "'The cache is in memory'",
                       '# The cache is in memory']:
            with self.subTest(source=source):
                with self.assertRaises(BatchValidationError) as caught:
                    validate_translation_content({'0': source}, [source])
                self.assertEqual(caught.exception.category, 'translation_untranslated')

    def test_code_syntax_warnings_do_not_expose_source_text(self):
        from subtitle_tool.mt_quality import validate_translation_content
        source = r"for key in '\data':"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            validate_translation_content({'0': source}, [source])
        self.assertFalse(caught)


class UntranslatedPipelineTests(PoolPipelineFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_workers = 1

    def test_copied_draft_never_publishes_even_when_reviewer_is_unavailable(self):
        self.install()
        self.draft_text = lambda fragment: fragment['text']
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'failed')
        self.assertFalse(self.source.with_suffix('.srt').exists())
        self.assertFalse(any(c['translated'] for c in self.store.cues(self.job_id)))
        self.assertFalse(any(row['stage'] == 'translate' for row in self.store.mt_stages(self.job_id)))
        self.assertFalse(any(stage in {'review', 'judge'} for stage, _, _ in self.calls))
        self.assertEqual(sum(stage == 'translate' for stage, _, _ in self.calls), 4)
        self.assertEqual(self.store.counts(self.job_id)['mt_invalid_response'], 4)
        self.assertFalse(any(json.loads(row[0])['value'].get('translations')
                             for row in self.store.db.execute('SELECT text FROM mt_cache')))

    def test_preserved_code_reaches_review_and_can_be_retimed_without_retry(self):
        sources = ['for key in data:', 'for i in range(10):', 'if value is not None:']
        self.save_result(result(*[(text, 'en') for text in sources]))
        engine = Engine(self.config, self.store)
        engine.reflow(self.job_id)
        self.draft_text = lambda fragment: fragment['text']
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['glossary', 'translate', 'review'])
        self.assertEqual([cue['translated'] for cue in self.store.cues(self.job_id)], sources)
        self.assertEqual(self.store.counts(self.job_id).get('mt_invalid_response', 0), 0)
        self.assertEqual(self.store.counts(self.job_id).get('mt_paid_fallback_success', 0), 0)
        before = self.source.with_suffix('.srt').read_bytes()
        self.calls.clear()
        engine.retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(self.calls)
        self.assertEqual(self.source.with_suffix('.srt').read_bytes(), before)

    def test_paid_retry_replaces_copied_prose_and_resume_reuses_the_good_result(self):
        self.install()
        state = {}
        self.stage_hook = lambda stage, data: state.update(model=data['_model'])
        self.draft_text = lambda fragment: (fragment['text'] if state['model'] == 'group/transcribe'
                                            else '缓存保存结果。' if fragment['id'] == '0' else '下次调用会复用它。')
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([data['_model'] for stage, _, data in self.calls if stage == 'translate'],
                         ['group/transcribe', 'group/transcribe', 'group/transcribe-paid'])
        self.assertEqual([c['translated'] for c in self.store.cues(self.job_id)], ['缓存保存结果。', '下次调用会复用它。'])
        self.assertEqual(self.store.counts(self.job_id)['mt_paid_fallback_success'], 1)
        engine = Engine(self.config, self.store)
        engine.retime(self.job_id, overwrite=True)
        self.calls.clear()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(self.calls)

    def test_old_quality_contract_keeps_saved_content_available_for_local_retime(self):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['execution_snapshot'].pop('content_validation_version', None)
        self.store.update(self.job_id, manifest=manifest)
        self.draft_text = lambda fragment: fragment['text']
        self.assertEqual(self.run_pool(), 'completed')
        before = self.source.with_suffix('.srt').read_bytes()
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.calls.clear()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertFalse(self.calls)
        self.assertEqual(self.source.with_suffix('.srt').read_bytes(), before)
