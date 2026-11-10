import json
import threading
from types import SimpleNamespace
from unittest import mock

from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_decisions import evaluate, metrics, probability, samples
from subtitle_tool.mt_transport import HTTPReply
from tests.fixtures import TemporaryTest


class DecisionTests(TemporaryTest):
    def test_balanced_golden_cases_and_injection_are_present(self):
        cases = samples()
        self.assertEqual(len(cases), 32)
        self.assertEqual(len({c['id'] for c in cases}), 32)
        self.assertEqual(sum(c['expected'] for c in cases), 16)
        self.assertEqual(len({c['category'] for c in cases}), 8)
        self.assertTrue(any('Ignore previous' in c['source'] for c in cases))

    def test_confidence_is_bounded_and_not_a_boolean_or_nonfinite_value(self):
        for value in (True, None, float('nan'), float('inf'), -1, 1.1, '0.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probability({'answers': {'error': {'noul': value}}})
        self.assertEqual(probability({'answers': {'error': {'noul': .8}}}), .8)

    def test_api_failure_is_not_counted_as_false_negative_and_stops_repeated_requests(self):
        config = SimpleNamespace(data=self.path, settings=SimpleNamespace(mt_base_url='http://offline/v1', mt_review_model='group/review'),
                                 get_magpie_key=lambda: 'private-key')
        transport = mock.Mock()
        def request(method, url, **kwargs):
            if url.endswith('/systemone'):
                return HTTPReply(403, b'private-service-error')
            return HTTPReply(200, json.dumps({'model': 'synthetic/reviewer', 'choices': [{'finish_reason': 'stop',
                'message': {'content': '{"issues":[]}'}}]}).encode())
        transport.request.side_effect = request
        report = evaluate(config, MTControl(threading.Event()), ('decider/model',), transport=transport)
        summary = report['models']['decider/model']['summary']
        self.assertEqual(summary['errors'], 1)
        self.assertEqual(summary['skipped'], 31)
        self.assertIsNone(summary['miss_rate'])
        self.assertEqual(transport.request.call_count, 33)
        self.assertFalse(report['production_changed'])
        text = json.dumps(report)
        self.assertNotIn('private', text)
        self.assertNotIn('Ignore previous', text)

    def test_confusion_matrix_uses_only_completed_cases(self):
        rows = [{'status': 'ok', 'expected': expected, 'predicted': predicted, 'duration_ms': 10}
                for expected, predicted in ((True, True), (True, False), (False, True), (False, False))]
        rows += [{'status': 'error'}, {'status': 'skipped'}]
        result = metrics(rows)
        self.assertEqual(result['miss_rate'], .5)
        self.assertEqual(result['false_positive_rate'], .5)
        self.assertEqual(result['completed'], 4)
