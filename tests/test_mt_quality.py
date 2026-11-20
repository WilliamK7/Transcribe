"""Stage and quality contract tests using only fictional course material."""
import json
import unittest

from subtitle_tool.common import ToolError
from subtitle_tool.mt_backend import MTBackend, key_fingerprint
from subtitle_tool.mt_quality import (make_stage_payload, parse_stage_response, plan_sentence_groups,
                                      request_cache_key, stage_output_size, estimate_stage_output,
                                      manual_terms, BatchValidationError)


def source(index, *, language='en', decision='translate', origin='automatic', segment='0000', start=None):
    start = index * 5000 if start is None else start
    return {'sentence_id': f'{segment}:{index:08}', 'text': f'Cache number {index}.',
            'language': language, 'decision': decision, 'decision_origin': origin, 'source_lang': 'English',
            'begin_ms': start, 'end_ms': start + 1000}


def body(value):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value, ensure_ascii=False)}}]}


class QualityContractTests(unittest.TestCase):
    def setUp(self):
        self.free = MTBackend('magpie', 'http://127.0.0.1:3425/v1', 'group/transcribe-free', key_fingerprint('gateway'), 5)
        self.paid = MTBackend('magpie', 'http://127.0.0.1:3425/v1', 'group/transcribe', key_fingerprint('gateway'), 5)
        self.free_caps = {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}
        self.paid_caps = dict(self.free_caps)
        self.scope = {'account': 'synthetic'}

    def test_groups_respect_sentence_count_span_segment_language_and_human_decision(self):
        records = [source(i) for i in range(9)] + [source(9, language='fr'), source(10, origin='manual'),
                   source(11, decision='keep'), source(12), source(13, segment='0001')]
        groups = plan_sentence_groups(records)
        self.assertEqual([len(group) for group in groups], [8, 1, 1, 1, 1, 1])
        self.assertNotIn('0000:00000011', [s['sentence_id'] for g in groups for s in g])
        groups = plan_sentence_groups([source(0, start=0), source(1, start=59001), source(2, start=60001)])
        self.assertEqual([len(group) for group in groups], [1, 2])

    def test_manual_terms_require_source_evidence(self):
        records = [source(0)]
        terms = manual_terms(records, {'cache': '缓存', 'CACHE': '其他译法', 'network': '网络'})
        self.assertEqual(terms, [{'source': 'cache', 'translation': '缓存', 'sentence': records[0]['sentence_id'],
                                  'origin': 'manual'}])


    def test_review_requires_specific_numbered_evidence_and_never_echoes_text_in_error(self):
        valid = {'issues': [{'id': '0', 'category': 'number', 'evidence': 'The value differs.'}]}
        self.assertEqual(parse_stage_response('review', body(valid), ids=['0']), valid)
        invalid = {'issues': [{'id': '7', 'category': 'number', 'evidence': 'private evidence'}]}
        with self.assertRaises(ToolError) as error:
            parse_stage_response('review', body(invalid), ids=['0'])
        self.assertNotIn('private evidence', str(error.exception))
        self.assertEqual(error.exception.code, 'mt_batch_invalid')

    def test_length_finish_is_classified_for_every_stage_even_with_complete_json(self):
        for stage, value in [('glossary', {'terms': []}), ('review', {'issues': []}),
                             *[(name, {'translations': {'0': '完整译文'}}) for name in ('translate', 'judge')]]:
            for content in [json.dumps(value), '{"private-incomplete-output":']:
                response = {'choices': [{'finish_reason': 'length', 'message': {'content': content}}]}
                with self.subTest(stage=stage, complete=content == json.dumps(value)), self.assertRaises(BatchValidationError) as error:
                    parse_stage_response(stage, response, ids=['0'], originals=['Synthetic input.'])
                self.assertEqual(error.exception.category, 'output_truncated')
                self.assertNotIn('private', str(error.exception))

    def test_output_workload_tracks_targets_and_translations_without_context_or_glossary(self):
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'context': [], 'glossary': [], 'fragments': [dict(item, sentence=item['id']) for item in [{'id': '0', 'text': 'Cache.'}]]}
        size = stage_output_size('translate', data)
        self.assertEqual(stage_output_size('translate', {**data, 'context': [{'text': 'Background ' * 1000}],
                                                    'glossary': [{'source': 'Cache', 'translation': '缓存'}]}), size)
        self.assertGreater(stage_output_size('translate', {**data, 'fragments': data['fragments'] * 2}), size)
        review = {**data, 'fragments': data['sentences'], 'translate': {'0': '缓存'}, 'translations': {'0': '缓存'}}
        self.assertEqual(stage_output_size('review', review), size)
        self.assertGreater(stage_output_size('review', {**review, 'translations': {'0': '长译文' * 100}}), size)

    def test_output_estimates_allow_for_list_fields_and_full_correction_translation(self):
        target = {'id': '0', 'text': 'A cache.'}
        data = {'sentences': [target], 'fragments': [{**target, 'sentence': '0'}],
                'translate': {'0': '整句译文'}, 'translations': {'0': '当前译文'}, 'context': [], 'glossary': []}
        draft = estimate_stage_output('translate', data)
        self.assertGreater(estimate_stage_output('glossary', data), draft)
        self.assertGreater(estimate_stage_output('review', data), draft)
        self.assertGreater(estimate_stage_output('judge', data), draft)
        expanded = {**data, 'translations': {'0': '更长的完整译文' * 100}}
        self.assertGreater(estimate_stage_output('judge', expanded), draft)
        self.assertEqual(estimate_stage_output('translate', {**data, 'context': [{'text': 'Other ' * 1000}]}), draft)

    def test_language_output_budget_depends_on_ids_and_count_instead_of_source_length(self):
        short = {'sentences': [{'id': '0', 'text': 'π/4.'}]}
        long = {'sentences': [{'id': '0', 'text': '(1+2)*' * 500}],
                'context': [{'text': 'Background. ' * 1000}]}
        estimate = estimate_stage_output('language', short)
        self.assertEqual(estimate_stage_output('language', long), estimate)
        self.assertLess(estimate, 4096 * 3 // 4)
        for targets in [[*short['sentences'], {'id': '1', 'text': '2'}],
                        [{'id': '123456789', 'text': '2'}]]:
            self.assertGreater(estimate_stage_output('language', {'sentences': targets}), estimate)
            for decision in ['original', 'translate', 'uncertain']:
                response = json.dumps({'decisions': {item['id']: decision for item in targets}}).encode()
                self.assertGreaterEqual(estimate_stage_output('language', {'sentences': targets}), len(response))

    def test_json_object_review_supplies_the_fields_and_categories_it_validates(self):
        from subtitle_tool.mt_quality import ISSUE_CATEGORIES
        data = {'fragments': [{'id': '0', 'text': 'One.'}, {'id': '1', 'text': 'Two.'}]}
        for fidelity in (False, True):
            with self.subTest(fidelity=fidelity):
                payload = make_stage_payload('review', data, self.free, capabilities=self.free_caps, fidelity=fidelity)
                schema = json.loads(payload['messages'][0]['content'].split('输出必须符合此 JSON Schema：', 1)[1])
                issue = schema['properties']['issues']['items']
                self.assertEqual(set(issue['required']), {'id', 'category', 'evidence'})
                self.assertEqual(issue['properties']['id']['enum'], ['0', '1'])
                self.assertEqual(issue['properties']['category']['enum'],
                                 list(ISSUE_CATEGORIES if fidelity else ISSUE_CATEGORIES[:-1]))

    def test_historical_request_cache_keys_survive_prompt_cleanup(self):
        backend = MTBackend('magpie', 'http://127.0.0.1:3425/v1', 'group/transcribe', key_fingerprint('synthetic'))
        data = {'sentences': [{'id': '0', 'text': 'A cache stores a result.'}],
                'fragments': [{'id': '0', 'sentence': '0', 'text': 'A cache stores a result.', 'display_ms': 1500}],
                'context': [], 'glossary': []}
        expected = {
            'language': 'f5007bf900b7e06fee24dafc3ec69b443f67006a9bb94f8e31350ae4c866039a',
            'glossary': '74493f5b0ded7e815de5650d64da84821dfe7ea9a21d6a8082e175e23556a5ec',
            'translate': '2da68b04fab9c271ede7b64ecda4165c189e59582bcf1d987a93a4f8061ee2bb',
            'review': 'd88def02cb200da1c9adb8412c10710fce3b791b408bc17853f5830467357dde',
            'judge': '4c33e84f0cec8781c92362ee7e111dcfb576351cb8f6a51f93a6b2c062a4ceac',
        }
        for stage, digest in expected.items():
            value = dict(data)
            if stage in {'review', 'judge'}:
                value['translations'] = {'0': '缓存保存结果。'}
            if stage == 'judge':
                value['issues'] = []
            with self.subTest(stage=stage):
                self.assertEqual(request_cache_key(stage, value, backend, capabilities=self.free_caps,
                                                  scope={'synthetic': 1}, stream=False), digest)
                self.assertNotEqual(request_cache_key(stage, value, backend, capabilities=self.free_caps,
                                                     scope={'synthetic': 1}, stream=False, fidelity=True), digest)


    def test_stage_cache_identity_covers_prompt_glossary_context_and_backend(self):
        data = {'sentences': [{'id': '0', 'text': 'A cache.'}], 'glossary': [], 'context': [], 'fragments': [dict(item, sentence=item['id']) for item in [{'id': '0', 'text': 'A cache.'}]]}
        original = request_cache_key('translate', data, self.free, capabilities=self.free_caps, scope=self.scope)
        self.assertNotEqual(original, request_cache_key('translate', {**data, 'glossary': [{'source': 'cache'}]},
                                                     self.free, capabilities=self.free_caps, scope=self.scope))
        self.assertNotEqual(original, request_cache_key('translate', {**data, 'context': ['different']},
                                                     self.free, capabilities=self.free_caps, scope=self.scope))
        self.assertNotEqual(original, request_cache_key('translate', data, self.paid,
                                                     capabilities=self.free_caps, scope=self.scope))

    def test_review_rejects_choice_error_refusal_and_unicode_surrogates(self):
        for extra in ({'error': {'code': 'synthetic'}}, {'message': {'content': '{"issues":[]}', 'refusal': 'denied'}}):
            response = body({'issues': []})
            response['choices'][0].update(extra)
            with self.assertRaises(ToolError):
                parse_stage_response('review', response, ids=['0'])
        response = body({'issues': [{'id': '0', 'category': 'meaning', 'evidence': '\ud800'}]})
        with self.assertRaises(ToolError):
            parse_stage_response('review', response, ids=['0'])

    def test_ascii_technical_terms_do_not_match_inside_unrelated_words(self):
        records = [{'sentence_id': '0000:00000000', 'text': 'The program runs.', 'decision': 'translate'}]
        self.assertEqual(manual_terms(records, {'RAM': '内存'}), [])

    def test_complete_payload_budget_includes_context_and_schema(self):
        data = {'sentences': [{'id': '0', 'text': 'Small.'}], 'context': ['x' * 33000], 'glossary': [], 'fragments': [dict(item, sentence=item['id']) for item in [{'id': '0', 'text': 'Small.'}]]}
        with self.assertRaises(ToolError) as error:
            make_stage_payload('translate', data, self.paid, capabilities=self.paid_caps)
        self.assertEqual(error.exception.code, 'mt_input_budget')

    def test_saved_capability_controls_request_size_and_cache_identity(self):
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'glossary': [], 'fragments': [dict(item, sentence=item['id']) for item in [{'id': '0', 'text': 'Cache.'}]]}
        caps = {'response_format': 'json_object', 'input_bytes': 2000, 'output_tokens': 512}
        payload = make_stage_payload('translate', data, self.free, capabilities=caps)
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['max_tokens'], 512)
        self.assertNotEqual(request_cache_key('translate', data, self.free, capabilities=self.free_caps, scope=self.scope),
                            request_cache_key('translate', data, self.free, capabilities=caps, scope=self.scope))

    def test_stage_texts_are_simplified_before_use_and_cache_identity_includes_scope(self):
        translated = parse_stage_response('translate', body({'translations': {'0': '繁體翻譯'}}),
                                          ids=['0'], originals=['Example'])
        self.assertEqual(translated['translations']['0'], '繁体翻译')
        terms = parse_stage_response('glossary', body({'terms': [
            {'source': 'cache', 'translation': '緩存', 'sentence': '0000:00000000'}]}),
            ids=['0000:00000000'], originals=['A cache stores results.'])
        self.assertEqual(terms['terms'][0]['translation'], '缓存')
        data = {'sentences': [{'id': '0', 'text': 'A cache.'}], 'glossary': [], 'fragments': [dict(item, sentence=item['id']) for item in [{'id': '0', 'text': 'A cache.'}]]}
        self.assertNotEqual(request_cache_key('translate', data, self.free, capabilities=self.free_caps, scope={'account': 'a'}),
                            request_cache_key('translate', data, self.free, capabilities=self.free_caps, scope={'account': 'b'}))

    def test_translation_mapping_rejects_duplicate_missing_extra_and_empty_results(self):
        invalid = ['{"translations":{"0":"甲","0":"乙","1":"丙"}}',
                   '{"translations":{"0":"甲"}}',
                   '{"translations":{"0":"甲","1":"乙","2":"丙"}}',
                   '{"translations":{"0":"甲","1":" "}}']
        for content in invalid:
            response = body({})
            response['choices'][0]['message']['content'] = content
            with self.subTest(content=content), self.assertRaises(ToolError):
                parse_stage_response('translate', response, ids=['0', '1'], originals=['First.', 'Second.'])
        response = body({'translations': {'1': '第二句', '0': '第一句'}})
        self.assertEqual(parse_stage_response('translate', response, ids=['0', '1'], originals=['First.', 'Second.']),
                         {'translations': {'0': '第一句', '1': '第二句'}})
        response['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ToolError):
            parse_stage_response('translate', response, ids=['0', '1'], originals=['First.', 'Second.'])

    def test_single_long_sentence_never_creates_a_group_over_sixty_seconds(self):
        from subtitle_tool.config import Settings
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.subtitles import normalize, plan_fragments
        from tests.fixtures import timed_raw
        raw = timed_raw([f'word{i}' for i in range(70)], word_ms=1000)
        source = normalize(raw, 0, '0000', duration_ms=80000)[0][0]
        cues = plan_fragments(source, Settings())
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.display_durations = {}
        pipeline._fit = lambda stage, data, **kwargs: data
        groups = pipeline.plan_groups([source], cues)
        self.assertGreater(len(groups), 1)
        for group in groups:
            members = [cue for unit in group for cue in unit['cues']]
            self.assertLessEqual(members[-1]['end_ms'] - members[0]['begin_ms'], 60000)
