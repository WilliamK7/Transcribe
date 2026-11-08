"""End-to-end v5 orchestration with synthetic source and a fake transport."""
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
import io

from subtitle_tool.engine import Engine
from subtitle_tool.mt_transport import HTTPReply
from subtitle_tool.common import ToolError
from tests.fixtures import result, timed_raw
from tests.mt_fixtures import PoolPipelineFixture


class PoolPipelineTests(PoolPipelineFixture):
    def set_input_budget(self, entry_id, size):
        manifest = self.store.job(self.job_id)['manifest']
        for entry in manifest['mt_route_snapshot']['entries']:
            if entry['id'] == entry_id:
                entry['capabilities']['input_bytes'] = size
        self.store.update(self.job_id, manifest=manifest)

    def test_unchanged_stage_reads_each_cache_miss_once(self):
        self.install()
        with mock.patch.object(self.store, 'cache_get', wraps=self.store.cache_get) as lookup:
            self.assertEqual(self.run_pool(), 'completed')
        keys = [call.args[0] for call in lookup.call_args_list]
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(set(keys)), 6)
        self.assertEqual(len(keys), 6)

    def test_cli_sigint_saves_inflight_draft_and_starts_no_later_stage(self):
        import _thread
        from subtitle_tool.main import main
        self.install()
        observed = threading.Event()
        request_cancel = Engine.request_cancel
        def cancel(engine, **kwargs):
            request_cancel(engine, **kwargs)
            observed.set()
        def interrupt(stage, data):
            if stage == 'draft':
                _thread.interrupt_main()
                self.assertTrue(observed.wait(3))
        self.stage_hook = interrupt
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), \
                mock.patch.object(Engine, 'request_cancel', new=cancel), \
                mock.patch('sys.stdout', new_callable=io.StringIO), \
                mock.patch('sys.stderr', new_callable=io.StringIO):
            code = self.run_pool(run=lambda: main(['--resume', self.job_id, '--non-interactive']))
        self.assertEqual(code, 130)
        self.assertEqual(self.store.job(self.job_id)['state'], 'cancelled')
        self.assertEqual([stage for stage, _, _ in self.calls], ['glossary', 'draft'])
        self.assertEqual({row['stage'] for row in self.store.mt_stages(self.job_id)}, {'glossary', 'semantic_input', 'draft'})

    def test_review_cache_prefers_other_backend_even_when_paid_network_is_denied(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.config.settings.mt_routing = {'paid_enabled': False}
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'draft': {'0': '缓存。'},
                'fragments': [{'id': '0', 'sentence': '0', 'text': 'Cache.'}],
                'translations': {'0': '缓存。'}, 'glossary': []}
        cached = json.dumps({'value': {'issues': []}, 'actual_model': 'synthetic', 'usage': None})
        with mock.patch.object(self.store, 'cache_get', return_value=cached), \
                mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('Unexpected HTTP')):
            value, origin, _ = pipeline.stage('review', data, ids=['0'], prefer='magpie_free')
        self.assertEqual(value, {'issues': []})
        self.assertEqual(origin, 'magpie')
        self.assertEqual(self.store.counts(self.job_id), {'mt_v5_review_cache_hit': 1})

    def test_trimmed_input_can_still_reuse_its_exact_persistent_cache(self):
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.mt_quality import stage_cache_key
        self.install()
        self.set_input_budget('magpie_free', 3000)
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'context': [], 'glossary': []}
        entry = pipeline.router.entries[0]
        key = stage_cache_key('draft', data, entry.backend,
                              capabilities=pipeline.profiles[entry.id]['capabilities'], scope=pipeline.scope)
        record = {'value': {'translations': {'0': '缓存。'}}, 'actual_model': 'synthetic', 'usage': None}
        with self.store.db:
            self.store.db.execute('INSERT INTO mt_cache VALUES(?,?)', (key, json.dumps(record)))
        data['context'] = [{'position': 'before', 'text': 'synthetic background ' * 200}]
        with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('Unexpected HTTP')):
            value, origin, _ = pipeline.stage('draft', data, ids=['0'], originals=['Cache.'])
        self.assertEqual(value, record['value'])
        self.assertEqual(origin, 'magpie_free')
        self.assertEqual(self.store.counts(self.job_id), {'mt_v5_draft_cache_hit': 1})
        self.assertTrue(data['context'])

    def test_input_trimming_preserves_caller_data_on_success_and_failure(self):
        import copy
        from subtitle_tool.mt_pipeline import Pipeline
        self.install()
        self.set_input_budget('magpie_free', 3000)
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        for text, succeeds in [('Cache.', True), ('Target text. ' * 1000, False)]:
            data = {'sentences': [{'id': '0', 'text': text}],
                    'context': [{'text': 'Background. ' * 1000}],
                    'glossary': [{'source': 'Cache', 'translation': 'synthetic term ' * 1000}]}
            original = copy.deepcopy(data)
            with self.subTest(succeeds=succeeds):
                if succeeds:
                    fitted = pipeline._fit('draft', data)
                    self.assertEqual(fitted, {'sentences': data['sentences'], 'context': [], 'glossary': []})
                else:
                    with self.assertRaises(ToolError) as error:
                        pipeline._fit('draft', data)
                    self.assertEqual(error.exception.code, 'mt_input_budget')
                self.assertEqual(data, original)

    def test_stages_complete_and_record_private_provenance(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        names = [stage for stage, _, _ in self.calls]
        self.assertEqual(names.count('glossary'), 1)
        self.assertEqual(names.count('draft'), 1)
        self.assertEqual(names.count('align'), 0)
        self.assertEqual(names.count('review'), 1)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertTrue(all(item['record'].get('backend') for item in self.store.mt_stages(self.job_id)
                            if item['stage'] != 'semantic_input'))
        for cue in self.store.cues(self.job_id):
            final = self.store.mt_stage_entry(self.job_id, cue['mt_stage_key'])['record']
            production = self.store.mt_stage_entry(self.job_id, final['production_stage_key'])
            self.assertEqual(production['stage'], 'draft')
            self.assertEqual(final['backend'], cue['mt_origin'])
            self.assertEqual(final['actual_model'], production['record']['actual_model'])
            self.assertEqual(final['backend'], production['record']['backend'])


    def test_first_group_is_saved_while_later_glossary_request_is_blocked(self):
        self.save_result(result(*[(f'Technical sentence {i}.', 'en') for i in range(9)]))
        Engine(self.config, self.store).reflow(self.job_id)
        blocked, release, first_saved = threading.Event(), threading.Event(), threading.Event()
        original_commit = self.store.commit_mt_stage

        def hook(stage, data):
            if stage == 'glossary' and any('sentence 8' in item['text'] for item in data['sentences']):
                blocked.set()
                release.wait(10)

        def commit(job_id, key, stage, record, **kwargs):
            original_commit(job_id, key, stage, record, **kwargs)
            if stage == 'final':
                first_saved.set()

        self.stage_hook = hook
        outcome = []
        def run():
            try:
                outcome.append(self.run_pool())
            except BaseException as error:
                outcome.append(error)

        with mock.patch.object(self.store, 'commit_mt_stage', side_effect=commit):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                reached_later_group = blocked.wait(10)
                saved_while_blocked = first_saved.wait(3)
            finally:
                release.set()
                worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertTrue(reached_later_group)
        self.assertTrue(saved_while_blocked, 'later glossary work blocked every complete subtitle group')
        self.assertEqual(outcome, ['completed'])

    def test_slow_first_glossary_releases_workers_for_bounded_lookahead(self):
        raw = result(*[(f'Technical sentence {i}.', 'en') for i in range(64)])
        for i, sentence in enumerate(raw['transcripts'][0]['sentences']):
            sentence['begin_time'], sentence['end_time'] = i * 100, i * 100 + 90
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        blocked, release, lookahead, too_far = (threading.Event() for _ in range(4))

        def hook(stage, data):
            if stage == 'glossary':
                first = data['sentences'][0]['text']
                if first == 'Technical sentence 0.':
                    blocked.set()
                    self.assertTrue(release.wait(10))
                elif first == 'Technical sentence 24.':
                    lookahead.set()
                elif first == 'Technical sentence 32.' and not release.is_set():
                    too_far.set()

        self.stage_hook = hook
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self.run_pool()))
        worker.start()
        try:
            reached_first = blocked.wait(5)
            advanced = lookahead.wait(2)
            unbounded = too_far.wait(.1)
            drafts_before_release = sum(stage == 'draft' for stage, _, _ in self.calls)
        finally:
            release.set()
            worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertTrue(reached_first)
        self.assertTrue(advanced, 'a worker waiting for an earlier glossary cannot prepare later groups')
        self.assertFalse(unbounded, 'glossary lookahead must stay within twice the worker count')
        self.assertEqual(drafts_before_release, 0)
        self.assertEqual(outcome, ['completed'])
        self.assertEqual(len(self.calls), 8 * 3)

    def test_prefetched_glossaries_keep_source_order_without_future_terms(self):
        raw = result(*[(f'Cache uses Marker0 Marker8 Marker16 in sentence {i}.', 'en') for i in range(24)])
        for i, sentence in enumerate(raw['transcripts'][0]['sentences']):
            sentence['begin_time'], sentence['end_time'] = i * 100, i * 100 + 90
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        ready = threading.Event()

        def group_index(data):
            return int(data['sentences'][0]['text'].rsplit(' ', 1)[1][:-1])

        def terms(data):
            index = group_index(data)
            sentence = data['sentences'][0]['id']
            return [{'source': 'Cache', 'translation': f'缓存{index}', 'sentence': sentence},
                    {'source': f'Marker{index}', 'translation': f'标记{index}', 'sentence': sentence}]

        def hook(stage, data):
            if stage == 'glossary':
                index = group_index(data)
                if index == 0:
                    self.assertTrue(ready.wait(5))
                elif index == 16:
                    ready.set()

        self.glossary_terms, self.stage_hook = terms, hook
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertEqual(len(drafts), 3)
        for data in drafts:
            index = group_index(data)
            actual = {(term['source'], term['translation']) for term in data['glossary']}
            self.assertEqual(actual, {pair for i in range(0, index + 1, 8)
                                      for pair in [('Cache', f'缓存{i}'), (f'Marker{i}', f'标记{i}')]})
            self.assertEqual(data['glossary'][0]['translation'], f'缓存{index}')

    def test_cancelling_at_prefix_dispatch_keeps_inflight_glossary(self):
        from subtitle_tool.mt_pipeline import GlossaryPrefix
        self.save_result(result(*[(f'Technical sentence {i}.', 'en') for i in range(9)]))
        Engine(self.config, self.store).reflow(self.job_id)
        engine = Engine(self.config, self.store)
        second_started, cancelled = threading.Event(), threading.Event()

        def hook(stage, data):
            if stage == 'glossary':
                if data['sentences'][0]['text'] == 'Technical sentence 0.':
                    self.assertTrue(second_started.wait(5))
                else:
                    second_started.set()
                    self.assertTrue(cancelled.wait(5))

        original = GlossaryPrefix.extend
        def consume(prefix, extracted):
            engine.request_cancel()
            cancelled.set()
            return original(prefix, extracted)

        self.stage_hook = hook
        with mock.patch.object(GlossaryPrefix, 'extend', new=consume):
            self.assertEqual(self.run_pool(engine=engine), 'cancelled')
        stages = self.store.mt_stages(self.job_id)
        self.assertEqual([stage['stage'] for stage in stages], ['glossary', 'glossary'])
        self.assertEqual(len(self.calls), 2)
        self.stage_hook = None
        Engine(self.config, self.store).resume(self.job_id)
        self.calls.clear()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])

    def test_review_service_failure_keeps_complete_translation_and_safe_warning(self):
        self.install()
        self.review_failure = True
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        warnings = self.store.job(self.job_id)['manifest']['warnings']
        self.assertTrue(any('复核' in warning for warning in warnings))
        self.assertFalse(any('A cache' in warning for warning in warnings))
        diagnostic = (self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')).read_text(encoding='utf-8')
        events = [json.loads(line) for line in diagnostic.splitlines()]
        self.assertTrue(any(event['kind'] == 'route_failure' and event.get('stage') == 'review' for event in events))
        self.assertNotIn('A cache', diagnostic)
        self.assertNotIn('qiniu-synthetic', diagnostic)


    def test_failed_advisory_glossary_is_not_requeried_when_resuming_a_draft(self):
        self.install()
        self.fail_stage = 'glossary'
        self.cancel_after = 'draft'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        markers = [row for row in self.store.mt_stages(self.job_id) if row['stage'] == 'glossary_unavailable']
        self.assertEqual(len(markers), 1)
        self.fail_stage = self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])
        self.assertNotIn('draft', [stage for stage, _, _ in self.calls])

    def test_glossary_prefix_matches_whole_terms_in_later_groups(self):
        self.config.settings.mt_workers = 1
        self.config.settings.mt_quality = {'manual_glossary': {'RAM': '内存'}}
        sentences = [('RAM stores data.', 'en')] + [('Wait.', 'en')] * 7 + [('The program runs.', 'en')]
        self.save_result(result(*sentences))
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertEqual(len(drafts), 2)
        self.assertEqual([term['source'] for term in drafts[0]['glossary']], ['RAM'])
        self.assertEqual(drafts[1]['glossary'], [])

    def test_glossary_prefix_reuses_evidenced_terms_with_case_and_punctuation(self):
        self.config.settings.mt_workers = 1
        self.config.settings.mt_quality = {'manual_glossary': {'RAM': '内存'}}
        sentences = [('RAM stores data.', 'en')] + [('Wait.', 'en')] * 7 + [('Use (ram).', 'en')]
        self.save_result(result(*sentences))
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertEqual(len(drafts), 2)
        self.assertEqual(drafts[1]['glossary'], drafts[0]['glossary'])

    def test_group_heartbeat_reports_activity_and_stops_after_scope(self):
        from subtitle_tool.mt_pipeline import GroupProgress
        from types import SimpleNamespace
        reported = threading.Event()
        events = []
        def progress(job_id, stage, done, total):
            events.append((stage, done))
            if len(events) >= 2:
                reported.set()
        progress = GroupProgress(SimpleNamespace(progress=progress), 'synthetic-job')
        progress.interval = .02
        progress.set(0, 'glossary')
        progress.start()
        try:
            self.assertTrue(reported.wait(2))
        finally:
            progress.close()
        self.assertFalse(progress.thread.is_alive())
        self.assertEqual(events[-1][1]['stages'], {'glossary': 1})

    def test_invalid_reply_records_safe_reason_without_output_text(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.install()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        response = {}
        class Transport:
            def request(self, method, url, **kwargs):
                kwargs['on_dispatch']()
                return HTTPReply(200, json.dumps(response).encode())
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()):
            pipeline = Pipeline(engine, self.job_id, manifest, scope)
            cases = [
                ('{"translations":{"9":"private-generated-text"}}', 'stop', 'id_out_of_range', 1),
                ('{"translations":{}}', 'stop', 'id_missing', 0),
                ('{"translations":{"0":"甲","0":"乙"}}', 'stop', 'json_duplicate_key', None),
                ('{"translations":{"0":" "}}', 'stop', 'translation_invalid', 1),
                ('{"translations":{"0":"完整译文"}}', 'length', 'output_truncated', 1),
                ('{"translations":', 'length', 'output_truncated', None),
                ('not JSON', 'stop', 'json_invalid', None),
                ('[]', 'stop', 'response_structure', None),
            ]
            for content, finish, _, _ in cases:
                response['choices'] = [{'finish_reason': finish, 'message': {'content': content}}]
                with self.subTest(finish=finish, content=content), self.assertRaises(ToolError):
                    pipeline.stage('draft', {'sentences': [{'id': '0', 'text': 'private-source-text'}],
                        'context': [], 'glossary': []}, ids=['0'], originals=['private-source-text'])
        log = (self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')).read_text(encoding='utf-8')
        events = [json.loads(line) for line in log.splitlines()]
        invalid = [event for event in events if event['kind'] == 'invalid_response']
        self.assertEqual([(event['category'], event['actual_count']) for event in invalid],
                         [(category, count) for _, _, category, count in cases])
        self.assertTrue(all(event['stage'] == 'draft' and event['expected_count'] == 1 for event in invalid))
        self.assertEqual(self.store.counts(self.job_id)['mt_output_truncated'], 2)
        failures = [event for event in events if event['kind'] == 'route_failure']
        self.assertEqual(sum(event['category'] == 'output_truncated' for event in failures), 2)
        self.assertEqual(self.store.mt_stages(self.job_id), [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM mt_cache').fetchone()[0], 0)
        self.assertNotIn('private-generated-text', log)
        self.assertNotIn('private-source-text', log)


    def test_two_repairs_stop_with_full_output_and_safe_warning(self):
        self.install()
        self.review_issue = True
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual([stage for stage, _, _ in self.calls].count('correct'), 2)
        self.assertEqual([stage for stage, _, _ in self.calls].count('review'), 3)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertTrue(any('复核' in warning for warning in self.store.job(self.job_id)['manifest']['warnings']))

    def test_correction_that_ignores_unchanged_reviewed_cues_stops_after_followup_review(self):
        self.install()
        self.review_issue = True
        self.corrected_translations = lambda data: {**data['translations'], '1': '只改了未被指出的另一句。'}
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls].count('correct'), 1)
        self.assertEqual([stage for stage, _, _ in self.calls].count('review'), 2)
        final = next(item['record'] for item in self.store.mt_stages(self.job_id) if item['stage'] == 'final')
        self.assertTrue(final['warning'])
        self.assertEqual(len(final['correction_stage_keys']), 1)
        self.assertEqual(self.store.counts(self.job_id)['mt_correction_stalled'], 1)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_stalled_correction_resumes_from_saved_followup_review_without_new_requests(self):
        self.install()
        self.review_issue = True
        self.corrected_translations = lambda data: {**data['translations'], '1': '只改了另一句。'}
        def hook(stage, data):
            if stage == 'review' and sum(name == 'review' for name, _, _ in self.calls) == 2:
                self.cancel_after = 'review'
        self.stage_hook = hook
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.calls.clear()
        self.stage_hook = self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.counts(self.job_id)['mt_correction_stalled'], 1)

    def test_stalled_detection_preserves_an_already_saved_second_correction(self):
        self.install()
        self.review_issue = True
        def corrected(data):
            if sum(stage == 'correct' for stage, _, _ in self.calls) == 2:
                self.cancel_after = 'correct'
                self.review_issue = False
                return {**data['translations'], '0': '第二轮已纠正了数字。'}
            return {**data['translations'], '1': '只改了另一句。'}
        self.corrected_translations = corrected
        with mock.patch('subtitle_tool.mt_pipeline.correction_stalled', return_value=False, create=True):
            self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['review'])
        final = next(item['record'] for item in self.store.mt_stages(self.job_id) if item['stage'] == 'final')
        self.assertEqual(len(final['correction_stage_keys']), 2)
        self.assertFalse(final['warning'])
        self.assertEqual(self.store.cues(self.job_id)[0]['translated'], '第二轮已纠正了数字。')

    def test_saved_only_stage_reuses_exact_cache_without_validation_or_network(self):
        from subtitle_tool.mt_pipeline import Pipeline
        from subtitle_tool.mt_quality import stage_cache_key
        self.install()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope)
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'draft': {'0': '缓存'},
                'fragments': [{'id': '0', 'sentence': '0', 'text': 'Cache.'}], 'glossary': [],
                'translations': {'0': '缓存'}, 'issues': [{'id': '0', 'category': 'meaning', 'evidence': '待复核'}]}
        candidate = pipeline.router.entries[0]
        key = stage_cache_key('correct', data, candidate.backend,
                              capabilities=pipeline.profiles[candidate.id]['capabilities'], scope=scope)
        with mock.patch.object(pipeline.transport, 'request', side_effect=AssertionError('Unexpected HTTP')), \
                mock.patch.object(pipeline, '_prepare_gateway', side_effect=AssertionError('Unexpected gateway validation')):
            self.assertIsNone(pipeline.stage('correct', data, ids=['0'], originals=['Cache.'], saved_only=True))
            cached = {'value': {'translations': {'0': '修正译文'}}, 'actual_model': 'synthetic', 'usage': None}
            with self.store.db:
                self.store.db.execute('INSERT INTO mt_cache VALUES(?,?)', (key, json.dumps(cached)))
            value, _, _ = pipeline.stage('correct', data, ids=['0'], originals=['Cache.'], saved_only=True)
        self.assertEqual(value, cached['value'])
        self.assertEqual(self.store.counts(self.job_id), {'mt_v5_correct_cache_hit': 1})


    def test_unchanged_correction_stops_without_repeating_identical_stage_evidence(self):
        self.install()
        self.review_issue = self.no_change_correction = True
        self.assertEqual(self.run_pool(), 'completed')
        finals = [item['record'] for item in self.store.mt_stages(self.job_id) if item['stage'] == 'final']
        self.assertEqual(len(finals), 1)
        final = finals[0]
        self.assertTrue(final['warning'])
        self.assertEqual(len(final['review_stage_keys']), 1)
        self.assertEqual(len(final['correction_stage_keys']), 1)
        self.assertEqual(final['production_stage_key'], final['correction_stage_keys'][0])
        self.assertTrue(all(cue['translated'] == '完整句译文。' for cue in self.store.cues(self.job_id)))
        calls = len(self.calls)
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(len(self.calls), calls)

    def test_cancelled_unchanged_correction_resumes_with_its_saved_result(self):
        self.install()
        self.review_issue = self.no_change_correction = True
        self.cancel_after = 'correct'
        self.assertEqual(self.run_pool(threading.Event()), 'cancelled')
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertIn('correct', [item['stage'] for item in self.store.mt_stages(self.job_id)])
        self.calls.clear()
        self.cancel_after = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed')
        self.assertEqual(self.calls, [])
        final = next(item['record'] for item in self.store.mt_stages(self.job_id) if item['stage'] == 'final')
        self.assertEqual(len(final['review_stage_keys']), 1)
        self.assertEqual(len(final['correction_stage_keys']), 1)

    def test_correction_cycle_stops_before_replaying_an_earlier_review(self):
        self.install()
        self.review_issue = True
        def cycle(stage, data):
            if stage == 'correct' and sum(name == 'correct' for name, _, _ in self.calls) == 2:
                self.no_change_correction = True
                data['translations'] = dict(data['draft'])  # Return the original direct draft.
        self.stage_hook = cycle
        self.assertEqual(self.run_pool(), 'completed')
        final = next(item['record'] for item in self.store.mt_stages(self.job_id) if item['stage'] == 'final')
        self.assertTrue(final['warning'])
        self.assertEqual(len(final['review_stage_keys']), 2)
        self.assertEqual(len(final['correction_stage_keys']), 2)
        self.assertEqual(final['production_stage_key'], final['correction_stage_keys'][-1])
        self.assertTrue(all(cue['translated'] == '完整句译文。' for cue in self.store.cues(self.job_id)))

    def test_failed_group_saves_no_partial_cues_and_resume_reuses_completed_stages(self):
        self.install(split=True)
        self.fail_stage = 'align'
        self.assertEqual(self.run_pool(), 'failed')
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertEqual([stage for stage, _, _ in self.calls].count('draft'), 1)
        self.fail_stage = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertNotIn('draft', [stage for stage, _, _ in self.calls])
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])

    def test_cancel_after_draft_starts_no_alignment_or_review(self):
        self.install()
        self.cancel_after = 'draft'
        cancel = threading.Event()
        self.assertEqual(self.run_pool(cancel), 'cancelled')
        self.assertNotIn('align', [stage for stage, _, _ in self.calls])
        self.assertNotIn('review', [stage for stage, _, _ in self.calls])
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_deny_paid_never_dispatches_to_paid_models(self):
        self.save_result(result(('Cache result.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        self.fail_stage = 'draft'
        self.assertEqual(self.run_pool(), 'failed')
        self.assertTrue(self.calls)
        self.assertFalse(any('deepseek' in data.get('_model', '') or 'maas.aliyuncs.com' in url
                             for _, url, data in self.calls))


    def test_local_integrity_error_in_review_does_not_become_soft_warning(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.install()
        original = Pipeline.stage
        def stage(pipeline, name, *args, **kwargs):
            if name == 'review':
                raise ToolError('保存的复核记录无效。', 'cache_mismatch')
            return original(pipeline, name, *args, **kwargs)
        with mock.patch.object(Pipeline, 'stage', stage):
            self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_router_propagates_unexpected_local_preparation_error(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'context': [], 'glossary': []}

        @contextmanager
        def invalid_preparation(*args):
            raise ToolError('Invalid saved data.', 'cache_mismatch')
            yield

        with mock.patch.object(Pipeline, '_prepare_gateway', invalid_preparation):
            with self.assertRaises(ToolError) as error:
                Pipeline(engine, self.job_id, manifest, scope).stage(
                    'draft', data, ids=['0'], originals=['Cache.'])
        self.assertEqual(error.exception.code, 'cache_mismatch')


    def test_oversized_single_sentence_uses_reliable_word_boundaries(self):
        self.config.settings.mt_workers = 1
        raw = timed_raw([f'cache{i:03}' for i in range(400)], word_ms=20)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertGreater(len(drafts), 1)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertEqual(''.join(item['text'] for draft in drafts for item in draft['sentences']),
                         raw['transcripts'][0]['sentences'][0]['text'])

    def test_deny_paid_budget_never_depends_on_larger_paid_model(self):
        self.config.settings.mt_workers = 1
        raw = timed_raw([f'cache{i:03}' for i in range(400)], word_ms=20)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertGreater(sum(stage == 'draft' for stage, _, _ in self.calls), 1)
        self.assertFalse(any(data['_model'] == 'group/transcribe' for _, _, data in self.calls))


    def test_retime_accepts_saved_route_origins_without_network(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        self.calls.clear()
        engine = Engine(self.config, self.store)
        engine.retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(self.calls, [])

    def test_retime_rejects_tampered_final_production_link(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        cue = self.store.cues(self.job_id)[0]
        final = self.store.mt_stage_entry(self.job_id, cue['mt_stage_key'])['record']
        final['production_stage_key'] = 'f' * 64
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(final), self.job_id, cue['mt_stage_key']))
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'cache_mismatch')

    def test_retime_rejects_changed_cue_not_matching_final_stage(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        cue = self.store.cues(self.job_id)[0]
        with self.store.db:
            self.store.db.execute('UPDATE cues SET translated=? WHERE job_id=? AND cue_id=?',
                                  ('被修改的译文', self.job_id, cue['cue_id']))
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'cache_mismatch')

    def test_retime_ignores_unused_direct_provider_key(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        self.assertTrue(all(cue['mt_origin'] == 'magpie_free' for cue in self.store.cues(self.job_id)))
        self.config.set_qiniu_key('changed-unused-qiniu')
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_retime_rejects_changed_used_key_before_saving_or_publishing(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        before = self.store.job(self.job_id)
        output = Path(before['output'])
        content = output.read_bytes()
        (self.config.data / 'magpie-key.txt').write_text('changed-used-magpie', encoding='utf-8')
        self.calls.clear()
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'mt_account_changed')
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(output.read_bytes(), content)
        self.assertFalse((self.config.root / '.build' / 'retime').exists())
        self.assertEqual(self.calls, [])

    def test_already_published_file_recovers_without_original_mt_key(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        self.store.update(self.job_id, 'failed', error='synthetic interruption', code='local_io')
        (self.config.data / 'magpie-key.txt').write_text('changed-after-publish', encoding='utf-8')
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])

    def test_unpublished_translation_still_requires_used_mt_key(self):
        self.install()
        self.assertEqual(self.run_pool(), 'completed')
        output = Path(self.store.job(self.job_id)['output'])
        output.unlink()
        self.store.update(self.job_id, 'failed', error='synthetic interruption', code='local_io')
        (self.config.data / 'magpie-key.txt').write_text('changed-before-republish', encoding='utf-8')
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_account_changed')

    def test_resume_with_a_previously_committed_group_validates_route_origin(self):
        self.config.settings.mt_workers = 1
        self.save_result(result(*[(f'Technical sentence {i}.', 'en') for i in range(9)]))
        Engine(self.config, self.store).reflow(self.job_id)
        self.fail_second_group = True
        self.assertEqual(self.run_pool(), 'failed')
        self.assertTrue(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.fail_second_group = False
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'completed', self.store.job(self.job_id)['error'])
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_tampered_saved_stage_metadata_blocks_reuse(self):
        self.install(split=True)
        self.fail_stage = 'align'
        self.assertEqual(self.run_pool(), 'failed')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'draft')
        row['record']['stage'] = 'review'
        row['record']['request_model'] = 'invalid-synthetic-model'
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(row['record']), self.job_id, row['key']))
        self.fail_stage = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')

    def test_non_string_actual_model_cannot_be_accepted_from_saved_stage(self):
        self.install(split=True)
        self.fail_stage = 'align'
        self.assertEqual(self.run_pool(), 'failed')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'draft')
        row['record']['actual_model'] = False
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(row['record']), self.job_id, row['key']))
        self.fail_stage = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_pool(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')

    def test_invalid_saved_values_block_stage_and_cross_task_cache_reuse(self):
        self.install(split=True)
        self.fail_stage = 'align'
        self.assertEqual(self.run_pool(), 'failed')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'draft')
        cache_key, cached = next((key, json.loads(value)) for key, value in
                                self.store.db.execute('SELECT key,text FROM mt_cache')
                                if json.loads(value)['value'] == row['record']['value'])
        self.fail_stage = None
        for storage in ['stage', 'cache']:
            for invalid in [None, [], {'translations': {'0': '甲'}},
                            {'translations': {'0': '甲', '1': ' '}},
                            {'translations': {'0': '甲', '1': '乙', '2': '多余'}}]:
                with self.subTest(storage=storage, invalid=invalid):
                    with self.store.db:
                        if storage == 'stage':
                            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                (json.dumps({**row['record'], 'value': invalid}), self.job_id, row['key']))
                        else:
                            self.store.db.execute('DELETE FROM mt_stages WHERE job_id=? AND stage_key=?',
                                                  (self.job_id, row['key']))
                            self.store.db.execute('UPDATE mt_cache SET text=? WHERE key=?',
                                                  (json.dumps({**cached, 'value': invalid}), cache_key))
                    self.calls.clear()
                    Engine(self.config, self.store).resume(self.job_id)
                    self.assertEqual(self.run_pool(), 'failed')
                    self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
                    self.assertEqual(self.calls, [])
                    self.assertFalse(self.source.with_suffix('.srt').exists())


    def test_denied_paid_still_reuses_exact_saved_stage_before_trimming_context(self):
        from subtitle_tool.mt_pipeline import Pipeline
        self.config.settings.mt_routing = {'paid_enabled': True}
        self.install()
        self.set_input_budget('magpie_free', 3000)
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        data = {'sentences': [{'id': '0', 'text': 'Cache.', 'source_language': 'English',
                               'asr_language': 'en', 'decision_origin': 'automatic'}],
                'context': [{'position': 'before', 'text': 'synthetic background ' * 200}], 'glossary': []}
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                if kwargs.get('on_dispatch'):
                    kwargs['on_dispatch']()
                calls.append(kwargs['json']['model'])
                body = {'model': kwargs['json']['model'], 'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'translations': {'0': '缓存。'}})}}]}
                return HTTPReply(200, json.dumps(body, ensure_ascii=False).encode('utf-8'))
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()):
            _, origin, _ = Pipeline(engine, self.job_id, manifest, scope).stage(
                'draft', data, ids=['0'], originals=['Cache.'])
            self.assertEqual(origin, 'magpie')
            manifest['mt_route_snapshot']['routing']['paid_enabled'] = False
            self.store.update(self.job_id, manifest=manifest)
            _, origin, _ = Pipeline(engine, self.job_id, manifest, scope).stage(
                'draft', data, ids=['0'], originals=['Cache.'])
            with self.store.db:
                self.store.db.execute('DELETE FROM mt_stages WHERE job_id=?', (self.job_id,))
            _, cached_origin, _ = Pipeline(engine, self.job_id, manifest, scope).stage(
                'draft', data, ids=['0'], originals=['Cache.'])
        self.assertEqual(origin, 'magpie')
        self.assertEqual(cached_origin, 'magpie')
        self.assertEqual(calls, ['group/transcribe'])


    def test_manual_language_change_does_not_reuse_previous_language_cache(self):
        from subtitle_tool.review import ReviewDecision
        self.install()
        engine = Engine(self.config, self.store)
        self.assertEqual(self.run_pool(engine=engine), 'completed')
        self.calls.clear()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['overwrite'] = True
        self.store.update(self.job_id, 'needs_review', manifest, code='language_uncertain')
        self.store.apply_review(self.job_id, self.store.cues(self.job_id)[0]['cue_id'],
                                ReviewDecision('translate', 'Japanese'))
        engine.finish_review(self.job_id)
        self.assertEqual(self.run_pool(engine=engine), 'completed', self.store.job(self.job_id)['error'])
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertTrue(drafts)
        self.assertTrue(any(item['source_language'] == 'Japanese' and item['decision_origin'] == 'manual'
                            for data in drafts for item in data['sentences']))
