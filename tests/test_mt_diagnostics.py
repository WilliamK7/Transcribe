from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


from subtitle_tool.mt_diagnostics import MTDiagnostics
from tests.fixtures import TemporaryTest


class DiagnosticTests(TemporaryTest):
    def test_pacing_diagnostics_keep_only_bounded_safe_fields(self):
        writer = MTDiagnostics(self.path, 'a' * 32)
        self.assertTrue(writer.write_route({'kind': 'route_pacing', 'entry': 'synthetic',
            'reason': 'rate_limit', 'concurrency': 2, 'interval_ms': 1000, 'error': 'private exception'}))
        self.assertTrue(writer.write_route({'kind': 'route_pacing', 'reason': 'private text',
                                           'concurrency': 100, 'interval_ms': 99999}))
        self.assertTrue(writer.write_route({'kind': 'route_failure', 'dispatched': False}))
        self.assertTrue(writer.write_route({'kind': 'route_failure', 'dispatched': 'private value'}))
        raw = writer.path.read_text(encoding='utf-8')
        first, second, rejected, invalid = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual((first['reason'], first['concurrency'], first['interval_ms']), ('rate_limit', 2, 1000))
        self.assertEqual(set(second), {'time', 'kind'})
        self.assertIs(rejected['dispatched'], False)
        self.assertNotIn('dispatched', invalid)
        self.assertNotIn('private', raw)

    def test_adaptive_selection_and_truncation_metadata_remain_content_free(self):
        writer = MTDiagnostics(self.path, 'a' * 32)
        for selection, size in [('weighted', 0), ('learning', 1), ('performance', 2), ('probe', 3),
                                ('private-value', 9000)]:
            self.assertTrue(writer.write_route({'kind': 'route_attempt', 'entry': 'synthetic',
                'selection': selection, 'size_bucket': size, 'text': 'private-source', 'key': 'private-key'}))
        self.assertTrue(writer.write_route({'kind': 'route_failure', 'category': 'output_truncated',
                                          'text': 'private-generated-text'}))
        self.assertTrue(writer.write({'kind': 'invalid_response', 'category': 'output_truncated',
                                      'finish_reason': 'length', 'content': 'private-generated-text'}))
        raw = writer.path.read_text(encoding='utf-8')
        records = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([row['selection'] for row in records[:4]], ['weighted', 'learning', 'performance', 'probe'])
        self.assertEqual([row['size_bucket'] for row in records[:4]], [0, 1, 2, 3])
        self.assertNotIn('selection', records[4])
        self.assertNotIn('size_bucket', records[4])
        self.assertEqual(records[-2]['category'], 'output_truncated')
        self.assertEqual(records[-1]['category'], 'output_truncated')
        self.assertNotIn('private', raw)

    def test_concurrent_whitelisted_content_free_diagnostics(self):
        job_id = 'a' * 32
        writers = [MTDiagnostics(self.path, job_id), MTDiagnostics(self.path, job_id)]
        event = {'kind': 'invalid_response', 'category': 'id_missing', 'expected_count': 3, 'actual_count': 1,
                 'group_key': 'f' * 64,
                 'stage': 'align', 'text': 'private transcript', 'error': 'private exception',
                 'key': 'sk-private', 'url': 'https://private.example/signature'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(lambda i: writers[i % 2].write(event), range(80))))
        raw = writers[0].path.read_text(encoding='utf-8')
        records = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(len(records), 80)
        self.assertTrue(all(r['actual_count'] == 1 and r['stage'] == 'align' for r in records))
        self.assertNotIn('private', raw)
        self.assertNotIn('https:', raw)

    def test_unsafe_ids_and_fields_are_dropped_and_io_failure_is_best_effort(self):
        writer = MTDiagnostics(self.path, 'a' * 32)
        for value in [None, [], {}, 'private text', 'sk-private', 'bearer-private',
                      'https://private.example', 'model\nprivate']:
            self.assertTrue(writer.write({'kind': 'invalid_response', 'model': value,
                                          'actual_count': 'private', 'group_key': 'private'}))
        raw = writer.path.read_text(encoding='utf-8')
        self.assertNotIn('private', raw)
        with mock.patch.object(Path, 'open', side_effect=OSError('private exception')):
            self.assertFalse(writer.write({'kind': 'invalid_response'}))
