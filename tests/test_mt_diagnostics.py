from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


from subtitle_tool.mt_diagnostics import MTDiagnostics
from tests.fixtures import TemporaryTest


class DiagnosticTests(TemporaryTest):
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
