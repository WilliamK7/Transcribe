"""Admission gates and measured reasoning with simulated API responses."""
import json
import threading
from types import SimpleNamespace
from unittest import mock

from subtitle_tool.mt_admission import CASES, ModelEvaluator
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_transport import HTTPReply, TransportError
from tests.fixtures import TemporaryTest


class AdmissionTests(TemporaryTest):
    def evaluator(self, *, reasoning=0, semantic=True, actual='candidate', judge='independent'):
        self.calls = []
        config = SimpleNamespace(data=self.path, settings=SimpleNamespace(mt_base_url='http://offline/v1'),
                                 get_magpie_key=lambda: 'synthetic-key')
        evaluator = ModelEvaluator(config, MTControl(threading.Event()), judge=judge)
        def request(model, stage, data):
            self.calls.append(stage)
            source = ' '.join(item['text'] for item in data['sentences'])
            case = next(case for case in CASES if case.source == source)
            value = {'translations': dict(zip([f['id'] for f in data['fragments']], case.translations, strict=True))}
            if stage in {'review', 'judge'}:
                value['issues'] = ([{'id': case.issue_id, 'category': case.category, 'evidence': '与原句含义不符。'}]
                                   if case.incorrect is not None else [])
                if stage == 'review':
                    del value['translations']
                    flagged = {issue['id'] for issue in value['issues']}
                    value['checks'] = {f['id']: 'issue' if f['id'] in flagged else 'ok' for f in data['fragments']}
            return {'model': actual, 'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(value, ensure_ascii=False)}}], 'usage': {
                'completion_tokens_details': {'reasoning_tokens': reasoning}}}
        evaluator.request = request
        evaluator.transport = mock.Mock()
        evaluator.transport.request.return_value = HTTPReply(200, json.dumps({'model': judge,
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'pass': [semantic] * 8})}}]}).encode())
        return evaluator

    def test_twelve_samples_and_independent_semantic_gate(self):
        evaluator = self.evaluator()
        result = evaluator.evaluate('provider/candidate')
        self.assertEqual(result['roles'], ['translate', 'review', 'judge'])
        self.assertEqual(len(self.calls), 12)
        self.assertTrue(all(result['non_thinking'].values()))
        evaluator.transport.request.assert_called_once()

    def test_admission_rejects_a_reviewer_without_per_screen_coverage(self):
        evaluator = self.evaluator()
        request = evaluator.request

        def missing_checks(model, stage, data):
            body = request(model, stage, data)
            if stage == 'review':
                message = body['choices'][0]['message']
                value = json.loads(message['content'])
                value.pop('checks')
                message['content'] = json.dumps(value)
            return body

        evaluator.request = missing_checks
        evaluator._judge = lambda cases, actual: [True] * len(cases)
        self.assertNotIn('review', evaluator.evaluate('candidate')['roles'])

    def test_complete_but_shifted_drafts_cannot_pass_on_batch_totals(self):
        evaluator = self.evaluator()
        request = evaluator.request

        def shifted(model, stage, data):
            body = request(model, stage, data)
            if stage == 'translate' and len(data['sentences']) > 1 and '12' in data['sentences'][-1]['text']:
                message = body['choices'][0]['message']
                value = json.loads(message['content'])
                keys = list(value['translations'])
                first, last = keys[0], keys[-1]
                value['translations'][first], value['translations'][last] = value['translations'][last], value['translations'][first]
                message['content'] = json.dumps(value)
            return body

        evaluator.request = shifted
        evaluator._judge = lambda cases, actual: [True] * len(cases)
        self.assertNotIn('translate', evaluator.evaluate('candidate')['roles'])

    def test_admission_exposes_a_model_that_stops_after_single_digit_subtitle_ids(self):
        evaluator = self.evaluator()
        request = evaluator.request

        def missing_tail(model, stage, data):
            body = request(model, stage, data)
            if stage == 'translate':
                message = body['choices'][0]['message']
                response = json.loads(message['content'])
                response['translations'].pop('10', None)
                message['content'] = json.dumps(response)
            return body

        evaluator.request = missing_tail
        evaluator._judge = lambda cases, actual: [True] * len(cases)
        report = evaluator.evaluate('candidate')
        self.assertNotIn('translate', report['roles'])
        self.assertTrue(any(failure['stage'] == 'translate' for failure in report['failures']))
        self.assertLessEqual(len(self.calls), 12)

    def test_wrong_sentence_fails_review_and_judgment_even_when_an_issue_is_reported(self):
        evaluator = self.evaluator()
        request = evaluator.request

        def misdirect(model, stage, data):
            body = request(model, stage, data)
            if stage in {'review', 'judge'} and len(data['sentences']) > 1:
                message = body['choices'][0]['message']
                value = json.loads(message['content'])
                if value['issues']:
                    value['issues'][0]['id'] = '0'
                    message['content'] = json.dumps(value)
            return body

        evaluator.request = misdirect
        with mock.patch.object(evaluator, '_judge', side_effect=lambda cases, actual: [True] * len(cases)):
            self.assertEqual(evaluator.evaluate('candidate')['roles'], ['translate'])

    def test_judge_must_correct_the_known_error_without_rewriting_the_control_sentence(self):
        for change in ('leave_error', 'rewrite_control', 'punctuate_neighbor', 'punctuate_error'):
            with self.subTest(change=change):
                evaluator = self.evaluator()
                request = evaluator.request

                def bad_fix(model, stage, data):
                    body = request(model, stage, data)
                    if stage == 'judge' and len(data['sentences']) > 1:
                        message = body['choices'][0]['message']
                        value = json.loads(message['content'])
                        if value['issues']:
                            if change == 'rewrite_control':
                                value['translations']['0'] = '缓存丢弃结果。'
                            else:
                                value['translations'] = dict(data['translations'])
                                if change == 'punctuate_neighbor':
                                    value['translations']['6'] += '。'
                                if change == 'punctuate_error':
                                    value['translations']['7'] += '！'
                            message['content'] = json.dumps(value, ensure_ascii=False)
                    return body

                evaluator.request = bad_fix
                with mock.patch.object(evaluator, '_judge', side_effect=lambda cases, actual: [True] * len(cases)):
                    self.assertEqual(evaluator.evaluate('candidate')['roles'], ['translate', 'review'])

    def test_semantic_rejection_cannot_admit_translation_or_correction(self):
        self.assertEqual(self.evaluator(semantic=False).evaluate('candidate')['roles'], ['review'])

    def test_same_underlying_judge_cannot_self_certify(self):
        evaluator = self.evaluator(actual='candidate-1017', judge='candidate')
        self.assertEqual(evaluator.evaluate('candidate')['roles'], ['review'])
        evaluator.transport.request.assert_not_called()

    def test_unknown_or_positive_reasoning_is_never_measured_as_off(self):
        for value in (None, 1, True):
            self.assertFalse(any(self.evaluator(reasoning=value).evaluate('candidate')['non_thinking'].values()))

    def test_temporary_judge_failure_is_not_cached_as_permanent_quality_failure(self):
        evaluator = self.evaluator()
        evaluator.transport.request.return_value = HTTPReply(429, b'upstream rate limit')
        result = evaluator.evaluate('candidate')
        self.assertIsNotNone(result['retry_after'])
        self.assertEqual(result['roles'], [])

    def test_rate_refusal_defers_whole_candidate_instead_of_asking_next_role(self):
        evaluator = self.evaluator()
        evaluator.request = mock.Mock(side_effect=TransportError('http_transient'))
        result = evaluator.evaluate('candidate')
        self.assertEqual(evaluator.request.call_count, 1)
        self.assertEqual(result['roles'], [])
        self.assertIsNotNone(result['retry_after'])
        evaluator.transport.request.assert_not_called()

    def test_unsupported_model_stops_all_roles_and_records_only_safe_code(self):
        evaluator = self.evaluator()
        evaluator.request = lambda *args: ModelEvaluator.request(evaluator, *args)
        evaluator.transport.request.return_value = HTTPReply(400, json.dumps({'error': {
            'code': 'model_not_supported', 'message': 'private response text'}}).encode())
        result = evaluator.evaluate('candidate')
        self.assertEqual(result['roles'], [])
        self.assertIsNone(result['retry_after'])
        self.assertEqual(evaluator.transport.request.call_count, 1)
        self.assertEqual(result['failures'][0]['service_code'], 'model_not_supported')
        self.assertNotIn('private', json.dumps(result))

    def test_transient_error_after_translation_never_admits_unjudged_output(self):
        evaluator = self.evaluator()
        request = evaluator.request
        def limited(model, stage, data):
            if stage == 'review':
                raise TransportError('http_transient')
            return request(model, stage, data)
        evaluator.request = limited
        result = evaluator.evaluate('candidate')
        self.assertEqual(result['roles'], [])
        self.assertIsNotNone(result['retry_after'])
        self.assertEqual(self.calls, ['translate'] * 4)
        evaluator.transport.request.assert_not_called()

    def test_evaluator_requests_allow_native_rpm_queue_and_have_unique_sessions(self):
        evaluator = self.evaluator()
        for _ in range(2):
            evaluator._judge([{'source': 'synthetic', 'translations': {'0': '合成'}}], set())
        calls = evaluator.transport.request.call_args_list
        self.assertGreaterEqual(calls[0].kwargs['timeout'], 120)
        self.assertNotEqual(calls[0].kwargs['headers']['X-Magpie-Session'],
                            calls[1].kwargs['headers']['X-Magpie-Session'])

    def test_candidate_requests_share_queue_timeout_and_unique_correlation(self):
        evaluator = self.evaluator()
        evaluator.transport.request.return_value = HTTPReply(200, b'{}')
        for _ in range(2):
            ModelEvaluator.request(evaluator, 'candidate', 'translate', {'sentences': [], 'fragments': []})
        calls = evaluator.transport.request.call_args_list
        self.assertGreaterEqual(calls[0].kwargs['timeout'], 120)
        self.assertNotEqual(calls[0].kwargs['headers']['X-Magpie-Session'],
                            calls[1].kwargs['headers']['X-Magpie-Session'])
