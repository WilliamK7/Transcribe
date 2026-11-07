"""Stage and quality contract tests using only fictional course material."""
import json
import unittest

from subtitle_tool.common import ToolError
from subtitle_tool.mt_backend import MTBackend, key_fingerprint
from subtitle_tool.mt_quality import (make_stage_payload, parse_stage_response, plan_sentence_groups,
                                      stage_cache_key, stage_output_size, estimate_stage_output, correction_stalled,
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
        self.qiniu = MTBackend('qiniu', 'https://api.qnaigc.com/v1', 'qiniu/conatus-free', key_fingerprint('q'), 5)
        self.router = MTBackend('openai_compatible', 'https://openrouter.ai/api/v1', 'openrouter/free', key_fingerprint('o'), 5)
        self.qiniu_caps = {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}
        self.router_caps = {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 4096}
        self.scope = {'account': 'synthetic'}

    def test_stalled_correction_requires_exact_issues_and_unchanged_affected_sentences(self):
        import copy
        previous = {'sentences': [], 'draft': {}, 'glossary': [],
                    'fragments': [{'id': '0', 'sentence': '0', 'text': 'First part.'},
                                  {'id': '1', 'sentence': '0', 'text': 'Second part.'},
                                  {'id': '2', 'sentence': '1', 'text': 'Other sentence.'}],
                    'translations': {'0': '首片', '1': '尾片', '2': '另一句'},
                    'issues': [{'id': '0', 'category': 'number', 'evidence': '值有误'}]}
        current = copy.deepcopy(previous)
        current['translations']['2'] = '修改另一句'
        self.assertTrue(correction_stalled(previous, current))
        self.assertFalse(correction_stalled(None, current))
        current['translations']['1'] = '同句的相邻片已修改'
        self.assertFalse(correction_stalled(previous, current))
        for name, value in [('id', '1'), ('category', 'meaning'), ('evidence', '新的具体证据')]:
            current = copy.deepcopy(previous)
            current['issues'][0][name] = value
            self.assertFalse(correction_stalled(previous, current))
        current = copy.deepcopy(previous)
        current['issues'] = []
        self.assertFalse(correction_stalled(previous, current))
        current['issues'] = previous['issues'] * 2
        self.assertTrue(correction_stalled(previous, current))


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

    def test_qiniu_and_openrouter_stage_wire_profiles_stay_separate(self):
        data = {'sentences': [{'id': '0', 'text': 'A cache stores a result.'}], 'glossary': []}
        q = make_stage_payload('draft', data, self.qiniu, capabilities=self.qiniu_caps)
        r = make_stage_payload('draft', data, self.router, capabilities=self.router_caps)
        self.assertEqual(q['response_format'], {'type': 'json_object'})
        self.assertNotIn('provider', q)
        self.assertNotIn('plugins', q)
        self.assertNotIn('enable_thinking', q)
        self.assertEqual(q['max_tokens'], 4096)
        self.assertEqual(r['response_format']['type'], 'json_schema')
        self.assertEqual(r['provider'], {'require_parameters': True})

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
                             *[(name, {'translations': {'0': '完整译文'}}) for name in ('draft', 'align', 'correct')]]:
            for content in [json.dumps(value), '{"private-incomplete-output":']:
                response = {'choices': [{'finish_reason': 'length', 'message': {'content': content}}]}
                with self.subTest(stage=stage, complete=content == json.dumps(value)), self.assertRaises(BatchValidationError) as error:
                    parse_stage_response(stage, response, ids=['0'], originals=['Synthetic input.'])
                self.assertEqual(error.exception.category, 'output_truncated')
                self.assertNotIn('private', str(error.exception))

    def test_output_workload_tracks_targets_and_translations_without_context_or_glossary(self):
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'context': [], 'glossary': []}
        size = stage_output_size('draft', data)
        self.assertEqual(stage_output_size('draft', {**data, 'context': [{'text': 'Background ' * 1000}],
                                                    'glossary': [{'source': 'Cache', 'translation': '缓存'}]}), size)
        self.assertGreater(stage_output_size('draft', {**data, 'sentences': data['sentences'] * 2}), size)
        review = {**data, 'fragments': data['sentences'], 'draft': {'0': '缓存'}, 'translations': {'0': '缓存'}}
        self.assertEqual(stage_output_size('review', review), size)
        self.assertGreater(stage_output_size('review', {**review, 'translations': {'0': '长译文' * 100}}), size)

    def test_output_estimates_allow_for_list_fields_and_full_correction_translation(self):
        target = {'id': '0', 'text': 'A cache.'}
        data = {'sentences': [target], 'fragments': [{**target, 'sentence': '0'}],
                'draft': {'0': '整句译文'}, 'translations': {'0': '当前译文'}, 'context': [], 'glossary': []}
        draft = estimate_stage_output('draft', data)
        self.assertGreater(estimate_stage_output('glossary', data), draft)
        self.assertGreater(estimate_stage_output('review', data), draft)
        self.assertEqual(estimate_stage_output('correct', data), draft)
        expanded = {**data, 'translations': {'0': '更长的完整译文' * 100}}
        self.assertGreater(estimate_stage_output('correct', expanded), draft)
        self.assertEqual(estimate_stage_output('draft', {**data, 'context': [{'text': 'Other ' * 1000}]}), draft)

    def test_json_object_review_supplies_the_fields_and_categories_it_validates(self):
        from subtitle_tool.mt_quality import ISSUE_CATEGORIES
        data = {'fragments': [{'id': '0', 'text': 'The value is two.'}]}
        payload = make_stage_payload('review', data, self.qiniu, capabilities=self.qiniu_caps)
        instruction = payload['messages'][0]['content']
        for field in ('issues', 'id', 'category', 'evidence', *ISSUE_CATEGORIES):
            self.assertIn('"' + field + '"', instruction)

    def test_review_schema_limits_evidence_to_actual_fragment_ids(self):
        data = {'fragments': [{'id': '0', 'text': 'One.'}, {'id': '1', 'text': 'Two.'}]}
        payload = make_stage_payload('review', data, self.router, capabilities=self.router_caps)
        schema = payload['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['issues']['items']['properties']['id']['enum'], ['0', '1'])

    def test_new_prompt_leaves_output_budget_for_json_on_slow_reasoning_routes(self):
        from dataclasses import replace
        data = {'sentences': [{'id': '0', 'text': 'A cache.'}], 'glossary': []}
        deepseek = replace(self.qiniu, model='deepseek/deepseek-v4.1-flash')
        for backend, capabilities, field, value in [
                (self.router, self.router_caps, 'reasoning', {'effort': 'low'}),
                (deepseek, self.qiniu_caps, 'reasoning_effort', 'low')]:
            with self.subTest(provider=backend.provider):
                current = make_stage_payload('draft', data, backend, capabilities=capabilities)
                self.assertEqual(current[field], value)
        custom = replace(self.qiniu, model='custom-model')
        payload = make_stage_payload('draft', data, custom, capabilities=self.qiniu_caps)
        self.assertNotIn('reasoning_effort', payload)
        fixed = replace(self.router, model='vendor/non-reasoning-model')
        payload = make_stage_payload('draft', data, fixed, capabilities=self.router_caps)
        self.assertNotIn('reasoning', payload)

    def test_stage_cache_identity_covers_prompt_glossary_context_and_backend(self):
        data = {'sentences': [{'id': '0', 'text': 'A cache.'}], 'glossary': [], 'context': []}
        original = stage_cache_key('draft', data, self.qiniu, capabilities=self.qiniu_caps, scope=self.scope)
        self.assertNotEqual(original, stage_cache_key('draft', {**data, 'glossary': [{'source': 'cache'}]},
                                                     self.qiniu, capabilities=self.qiniu_caps, scope=self.scope))
        self.assertNotEqual(original, stage_cache_key('draft', {**data, 'context': ['different']},
                                                     self.qiniu, capabilities=self.qiniu_caps, scope=self.scope))
        self.assertNotEqual(original, stage_cache_key('draft', data, self.router,
                                                     capabilities=self.qiniu_caps, scope=self.scope))

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
        data = {'sentences': [{'id': '0', 'text': 'Small.'}], 'context': ['x' * 33000], 'glossary': []}
        with self.assertRaises(ToolError) as error:
            make_stage_payload('draft', data, self.router, capabilities=self.router_caps)
        self.assertEqual(error.exception.code, 'mt_input_budget')

    def test_explicit_capability_controls_wire_format_and_size(self):
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'glossary': []}
        caps = {'response_format': 'text', 'input_bytes': 2000, 'output_tokens': 512}
        payload = make_stage_payload('draft', data, self.qiniu, capabilities=caps)
        self.assertNotIn('response_format', payload)
        self.assertEqual(payload['max_tokens'], 512)
        self.assertNotEqual(stage_cache_key('draft', data, self.qiniu, capabilities=self.qiniu_caps, scope=self.scope),
                            stage_cache_key('draft', data, self.qiniu, capabilities=caps, scope=self.scope))

    def test_stage_texts_are_simplified_before_use_and_cache_identity_includes_scope(self):
        translated = parse_stage_response('draft', body({'translations': {'0': '繁體翻譯'}}),
                                          ids=['0'], originals=['Example'])
        self.assertEqual(translated['translations']['0'], '繁体翻译')
        terms = parse_stage_response('glossary', body({'terms': [
            {'source': 'cache', 'translation': '緩存', 'sentence': '0000:00000000'}]}),
            ids=['0000:00000000'], originals=['A cache stores results.'])
        self.assertEqual(terms['terms'][0]['translation'], '缓存')
        data = {'sentences': [{'id': '0', 'text': 'A cache.'}], 'glossary': []}
        self.assertNotEqual(stage_cache_key('draft', data, self.qiniu, capabilities=self.qiniu_caps, scope={'account': 'a'}),
                            stage_cache_key('draft', data, self.qiniu, capabilities=self.qiniu_caps, scope={'account': 'b'}))

    def test_translation_mapping_rejects_duplicate_missing_extra_and_empty_results(self):
        invalid = ['{"translations":{"0":"甲","0":"乙","1":"丙"}}',
                   '{"translations":{"0":"甲"}}',
                   '{"translations":{"0":"甲","1":"乙","2":"丙"}}',
                   '{"translations":{"0":"甲","1":" "}}']
        for content in invalid:
            response = body({})
            response['choices'][0]['message']['content'] = content
            with self.subTest(content=content), self.assertRaises(ToolError):
                parse_stage_response('align', response, ids=['0', '1'], originals=['First.', 'Second.'])
        response = body({'translations': {'1': '第二句', '0': '第一句'}})
        self.assertEqual(parse_stage_response('align', response, ids=['0', '1'], originals=['First.', 'Second.']),
                         {'translations': {'0': '第一句', '1': '第二句'}})
        response['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ToolError):
            parse_stage_response('align', response, ids=['0', '1'], originals=['First.', 'Second.'])

    def test_single_long_sentence_never_creates_a_group_over_sixty_seconds(self):
        from subtitle_tool.config import Settings
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.subtitles import normalize, plan_fragments
        from tests.fixtures import timed_raw
        raw = timed_raw([f'word{i}' for i in range(70)], word_ms=1000)
        source = normalize(raw, 0, '0000', duration_ms=80000)[0][0]
        cues = plan_fragments(source, Settings())
        pipeline = Pipeline.__new__(Pipeline)
        pipeline._fit = lambda stage, data, **kwargs: data
        groups = pipeline.plan_groups([source], cues)
        self.assertGreater(len(groups), 1)
        for group in groups:
            members = [cue for unit in group for cue in unit['cues']]
            self.assertLessEqual(members[-1]['end_ms'] - members[0]['begin_ms'], 60000)


if __name__ == '__main__':
    unittest.main()
