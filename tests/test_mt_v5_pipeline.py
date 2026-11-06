"""End-to-end v5 orchestration with synthetic source and a fake transport."""
import json
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock
import io

from subtitle_tool.engine import Engine
from subtitle_tool.mt_quality import PROMPTS
from subtitle_tool.mt_transport import HTTPReply
from subtitle_tool.common import ToolError
from subtitle_tool.mt_routing import RouteFailure
from tests.fixtures import SavedResultFixture, result, timed_raw


class V5PipelineTests(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.set_qiniu_key('synthetic-qiniu')
        (self.config.data / 'mt-key.txt').write_text('synthetic-openrouter', encoding='utf-8')
        self.config.settings.mt_provider = 'pool'
        self.config.settings.mt_workers = 2
        self.calls = []
        self.review_failure = False
        self.review_issue = False
        self.fail_stage = None
        self.cancel_after = None
        self.fail_second_group = False
        self.quota_remaining = 1000
        self.quota_queries = 0
        self.stage_hook = None

    def run_v5(self, cancel=None, engine=None):
        parent = self
        class Transport:
            def request(self, method, url, **kwargs):
                if method == 'GET':
                    parent.quota_queries += 1
                    quota = {'data': {'free_model_daily_requests': {'used': 1000 - parent.quota_remaining,
                                                                    'limit': 1000, 'remaining': parent.quota_remaining}}}
                    return HTTPReply(200, json.dumps(quota).encode('utf-8'))
                if kwargs.get('on_dispatch') is not None:
                    kwargs['on_dispatch']()
                payload = kwargs['json']
                stage = next(name for name, prompt in PROMPTS.items() if payload['messages'][0]['content'].startswith(prompt))
                data = json.loads(payload['messages'][1]['content'])
                data['_model'] = payload['model']
                parent.calls.append((stage, url, data))
                if parent.stage_hook is not None:
                    parent.stage_hook(stage, data)
                if stage == parent.fail_stage:
                    return HTTPReply(400, b'{}')
                if stage == 'draft' and parent.fail_second_group and any(
                        'sentence 8' in item['text'] for item in data['sentences']):
                    return HTTPReply(400, b'{}')
                if stage == 'review' and parent.review_failure:
                    return HTTPReply(400, b'{}')
                if stage == 'glossary':
                    value = {'terms': []}
                elif stage == 'draft':
                    value = {'translations': {item['id']: '完整句译文。' for item in data['sentences']}}
                elif stage in {'align', 'correct'}:
                    suffix = str(sum(call[0] == 'correct' for call in parent.calls)) if stage == 'correct' else ''
                    value = {'translations': {item['id']: '字幕完整译文。' + suffix for item in data['fragments']}}
                else:
                    value = {'issues': ([{'id': '0', 'category': 'number', 'evidence': '数字有疑点'}]
                                        if parent.review_issue else [])}
                content = json.dumps(value, ensure_ascii=False)
                response = {'model': payload['model'], 'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
                            'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
                if stage == parent.cancel_after and cancel is not None:
                    cancel.set()
                return HTTPReply(200, json.dumps(response, ensure_ascii=False).encode('utf-8'))
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()), \
             mock.patch('subtitle_tool.mt_runtime.ProcessTransport', return_value=Transport()), \
             mock.patch('subtitle_tool.mt_backend.qiniu_free_active', return_value=True):
            return (engine or Engine(self.config, self.store, cancel)).run(self.job_id)

    def install(self):
        self.save_result(result(('A cache stores a result.', 'en'), ('The next call reuses it.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id)
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['translation_strategy_version'], 5)

    def test_stages_complete_and_record_private_provenance(self):
        self.install()
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        names = [stage for stage, _, _ in self.calls]
        self.assertEqual(names.count('glossary'), 1)
        self.assertEqual(names.count('draft'), 1)
        self.assertEqual(names.count('align'), 1)
        self.assertEqual(names.count('review'), 1)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertTrue(all(item['record'].get('backend') for item in self.store.mt_stages(self.job_id)))
        for cue in self.store.cues(self.job_id):
            final = self.store.mt_stage_entry(self.job_id, cue['mt_stage_key'])['record']
            production = self.store.mt_stage_entry(self.job_id, final['production_stage_key'])
            self.assertIn(production['stage'], {'align', 'correct'})
            self.assertEqual(final['backend'], cue['mt_origin'])
            self.assertEqual(final['actual_model'], production['record']['actual_model'])
            self.assertEqual(final['backend'], production['record']['backend'])

    def test_old_prompt_resumes_saved_draft_and_reflow_selects_new_prompt(self):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['execution_snapshot']['prompt_version'], 2)
        manifest['execution_snapshot']['prompt_version'] = 1
        self.store.update(self.job_id, manifest=manifest)
        cancel = threading.Event()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_v5(cancel), 'cancelled')
        saved = {item['key']: item for item in self.store.mt_stages(self.job_id)}
        self.assertEqual({item['stage'] for item in saved.values()}, {'glossary', 'draft'})
        self.cancel_after = None
        self.calls.clear()
        self.store.update(self.job_id, 'queued')
        self.assertEqual(self.run_v5(), 'completed')
        self.assertEqual([stage for stage, _, _ in self.calls], ['align', 'review'])
        self.assertEqual(self.store.job(self.job_id)['manifest']['execution_snapshot']['prompt_version'], 1)
        for key, item in saved.items():
            self.assertEqual(self.store.mt_stage_entry(self.job_id, key),
                             {'stage': item['stage'], 'record': item['record']})
        Engine(self.config, self.store).reflow(self.job_id, output_dir=self.path / 'new-prompt')
        self.assertEqual(self.store.job(self.job_id)['manifest']['execution_snapshot']['prompt_version'], 2)

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
                outcome.append(self.run_v5())
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

    def test_review_service_failure_keeps_complete_translation_and_safe_warning(self):
        self.install()
        self.review_failure = True
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        warnings = self.store.job(self.job_id)['manifest']['warnings']
        self.assertTrue(any('复核' in warning for warning in warnings))
        self.assertFalse(any('A cache' in warning for warning in warnings))
        diagnostic = (self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')).read_text(encoding='utf-8')
        events = [json.loads(line) for line in diagnostic.splitlines()]
        self.assertTrue(any(event['kind'] == 'route_failure' and event.get('stage') == 'review' for event in events))
        self.assertNotIn('A cache', diagnostic)
        self.assertNotIn('qiniu-synthetic', diagnostic)

    def test_old_partial_draft_retains_global_glossary_and_reuses_saved_stage(self):
        self.install()
        cancel = threading.Event()
        self.cancel_after = 'draft'
        self.assertEqual(self.run_v5(cancel), 'cancelled')
        manifest = self.store.job(self.job_id)['manifest']
        manifest.pop('mt_glossary_schedule')
        self.store.update(self.job_id, manifest=manifest)
        self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'completed')
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_glossary_schedule'], 'global-v1')
        self.assertNotIn('draft', [stage for stage, _, _ in self.calls])
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])

    def test_explicit_reflow_clears_old_glossary_schedule(self):
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['mt_glossary_schedule'] = 'global-v1'
        self.store.update(self.job_id, manifest=manifest)
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertIsNone(self.store.job(self.job_id)['manifest'].get('mt_glossary_schedule'))
        self.assertEqual(self.run_v5(), 'completed')
        self.assertEqual(self.store.job(self.job_id)['manifest']['mt_glossary_schedule'], 'prefix-v1')

    def test_failed_advisory_glossary_is_not_requeried_when_resuming_a_draft(self):
        self.install()
        self.fail_stage = 'glossary'
        self.cancel_after = 'draft'
        self.assertEqual(self.run_v5(threading.Event()), 'cancelled')
        markers = [row for row in self.store.mt_stages(self.job_id) if row['stage'] == 'glossary_unavailable']
        self.assertEqual(len(markers), 1)
        self.fail_stage = self.cancel_after = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'completed')
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])
        self.assertNotIn('draft', [stage for stage, _, _ in self.calls])

    def test_glossary_prefix_uses_source_order_and_wait_can_be_cancelled(self):
        from subtitle_tool.mt_pipeline_v5 import GlossaryPrefix
        from subtitle_tool.mt_control import MTControl
        from subtitle_tool.common import Cancelled
        from types import SimpleNamespace
        sources = [{'sentence_id': str(i), 'text': 'A cache.', 'decision': 'translate'} for i in range(2)]
        control = MTControl(threading.Event())
        pipeline = SimpleNamespace(engine=SimpleNamespace(mt_control=control),
            snapshot={'quality': {'manual_glossary': {}}}, group_progress=None)
        prefix = GlossaryPrefix(pipeline, 2, sources)
        later = [{'source': 'cache', 'translation': '后出现的术语', 'sentence': '1'}]
        outcome, waiting = [], threading.Event()
        def run():
            waiting.set()
            try:
                outcome.append(prefix.for_group(1, later))
            except Cancelled:
                outcome.append('cancelled')
        worker = threading.Thread(target=run)
        worker.start()
        waiting.wait(2)
        first = prefix.for_group(0, [{'source': 'cache', 'translation': '先出现的术语', 'sentence': '0'}])
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first[0]['translation'], '先出现的术语')
        self.assertEqual(outcome[0][0]['translation'], '先出现的术语')
        prefix = GlossaryPrefix(pipeline, 2, sources)
        outcome.clear()
        waiting.clear()
        worker = threading.Thread(target=run)
        worker.start()
        waiting.wait(2)
        control.cancel_user()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ['cancelled'])

    def test_group_heartbeat_reports_activity_and_stops_after_scope(self):
        from subtitle_tool.mt_pipeline_v5 import GroupProgress
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
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.install()
        engine = Engine(self.config, self.store)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'translations': {'9': 'private-generated-text'}})}}]}
        class Transport:
            def request(self, method, url, **kwargs):
                kwargs['on_dispatch']()
                return HTTPReply(200, json.dumps(response).encode())
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()):
            pipeline = Pipeline(engine, self.job_id, manifest, scope, 1)
            with self.assertRaises(ToolError):
                pipeline.stage('draft', {'sentences': [{'id': '0', 'text': 'private-source-text'}],
                    'context': [], 'glossary': []}, ids=['0'], originals=['private-source-text'])
        log = (self.config.root / '.build' / 'mt-diagnostics' / (self.job_id + '.jsonl')).read_text(encoding='utf-8')
        events = [json.loads(line) for line in log.splitlines()]
        invalid = next(event for event in events if event['kind'] == 'invalid_response')
        self.assertEqual(invalid['category'], 'id_out_of_range')
        self.assertEqual(invalid['stage'], 'draft')
        self.assertEqual(invalid['expected_count'], 1)
        self.assertNotIn('private-generated-text', log)
        self.assertNotIn('private-source-text', log)

    def test_console_login_failure_emits_one_actionable_progress_message(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.install()
        progress = mock.Mock()
        engine = Engine(self.config, self.store, progress=progress)
        manifest = self.store.job(self.job_id)['manifest']
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope, 1)
        for _ in range(3):
            pipeline._quota_event({'kind': 'runtime_quota', 'category': 'quota_console_session_invalid'})
        progress.assert_called_once_with(self.job_id, 'mt_quota_login', None, None)

    def test_two_repairs_stop_with_full_output_and_safe_warning(self):
        self.install()
        self.review_issue = True
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual([stage for stage, _, _ in self.calls].count('correct'), 2)
        self.assertEqual([stage for stage, _, _ in self.calls].count('review'), 3)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertTrue(any('复核' in warning for warning in self.store.job(self.job_id)['manifest']['warnings']))

    def test_failed_group_saves_no_partial_cues_and_resume_reuses_completed_stages(self):
        self.install()
        self.fail_stage = 'align'
        self.assertEqual(self.run_v5(), 'failed')
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertEqual([stage for stage, _, _ in self.calls].count('draft'), 1)
        self.fail_stage = None
        self.calls.clear()
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertNotIn('draft', [stage for stage, _, _ in self.calls])
        self.assertNotIn('glossary', [stage for stage, _, _ in self.calls])

    def test_cancel_after_draft_starts_no_alignment_or_review(self):
        self.install()
        self.cancel_after = 'draft'
        cancel = threading.Event()
        self.assertEqual(self.run_v5(cancel), 'cancelled')
        self.assertNotIn('align', [stage for stage, _, _ in self.calls])
        self.assertNotIn('review', [stage for stage, _, _ in self.calls])
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_deny_paid_never_dispatches_to_paid_models(self):
        self.save_result(result(('Cache result.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        self.fail_stage = 'draft'
        self.assertEqual(self.run_v5(), 'failed')
        self.assertTrue(self.calls)
        self.assertFalse(any('deepseek' in data.get('_model', '') or 'maas.aliyuncs.com' in url
                             for _, url, data in self.calls))

    def test_existing_bailian_free_route_without_verified_quota_never_dispatches(self):
        self.config.settings.mt_backends = [{'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen-test'}]
        self.save_result(result(('A cache stores a result.', 'en')))
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        self.assertEqual(self.run_v5(), 'failed')
        self.assertEqual(self.calls, [])

    def test_verified_synthetic_bailian_route_reserves_and_settles_usage(self):
        from subtitle_tool.mt_bailian_quota import BailianQuota, VerifiedIdentity
        self.config.settings.mt_backends = [{'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen-test'}]
        self.install()
        source = mock.Mock()
        source.bound_to.return_value = True
        source.query.return_value = json.dumps([{'model': 'qwen-test', 'type': 'Text', 'remaining': 18000,
            'total': 18000, 'remainingPercent': 100, 'expires': '2099-12-31', 'autoStop': True}]).encode()
        def quota(control, **kwargs):
            kwargs.update(source=source, protection=lambda route, row: True, observation=lambda proof, rows: None,
                          identity=lambda route: VerifiedIdentity('synthetic-account', 'offline-space',
                                                                 route.backend.key_fingerprint, 'cn-beijing', 'c' * 64))
            return BailianQuota(control, **kwargs)
        with mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True), \
             mock.patch('subtitle_tool.mt_bailian_quota.BailianQuota', side_effect=quota):
            self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(source.query.call_count, 1)
        self.assertGreaterEqual(len(self.calls), 4)
        self.assertTrue(all(data['_model'] == 'qwen-test' for _, _, data in self.calls))

    def test_cli_confirmed_binding_is_used_by_the_production_pipeline(self):
        from subtitle_tool.main import main
        from subtitle_tool.mt_bailian_quota import CLIQuotaSource
        self.config.settings.mt_backends = [{'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen3.8-27b',
                                             'capabilities': {'response_format': 'json_object'}}]
        self.install()
        config_dir = self.path / 'synthetic-cli'
        config_dir.mkdir()
        (config_dir / 'config.json').write_text(json.dumps({
            'access_token': 'synthetic-token', 'console_site': 'domestic', 'console_region': 'cn-beijing',
            'console_switch_agent': 123, 'workspace_id': 'offline-space', 'api_key': 'synthetic-cli-key'}),
            encoding='utf-8')
        source = CLIQuotaSource(config_dir=config_dir)
        source.query = mock.Mock(return_value=json.dumps([{
            'model': 'qwen3.8-27b', 'type': 'Text', 'remaining': 100000, 'total': 100000,
            'remainingPercent': 100, 'expires': '2099-12-31', 'autoStop': True}]).encode())
        with mock.patch('subtitle_tool.main.Config', return_value=self.config), \
             mock.patch('subtitle_tool.mt_bailian_binding.CLIQuotaSource', return_value=source), \
             mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--bind-bailian-account', '--confirm-same-account']), 0)
            self.assertEqual(main(['--confirm-bailian-protection', 'qwen3.8-27b',
                                   '--confirm-propagation-waited']), 0)
            self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertGreaterEqual(source.query.call_count, 2)
        self.assertGreaterEqual(len(self.calls), 4)
        self.assertTrue(all(data['_model'] == 'qwen3.8-27b' for _, _, data in self.calls))
        self.assertNotIn('synthetic-token', output.getvalue())

    def test_blocked_dispatch_is_not_counted_as_an_http_request(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [{'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen-test'}]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        sent = []
        class Reservation:
            def claim(self):
                pass
            def mark_sent(self):
                raise RouteFailure('quota_unknown')
        @contextmanager
        def reserve(*args):
            yield Reservation()
        class Transport:
            def request(self, method, url, **kwargs):
                kwargs['on_dispatch']()
                sent.append(url)
        data = {'sentences': [{'id': '0', 'text': 'Synthetic sentence.'}], 'context': [], 'glossary': []}
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()), \
             mock.patch.object(Pipeline, '_reserve_quota', reserve):
            with self.assertRaises(ToolError):
                Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                    'draft', data, ids=['0'], originals=['Synthetic sentence.'])
        self.assertEqual(sent, [])
        counts = self.store.counts(self.job_id)
        self.assertEqual(counts.get('mt', 0), 0)
        self.assertEqual(counts.get('mt_v5_bailian_paid_draft_request', 0), 0)

    def test_local_integrity_error_in_review_does_not_become_soft_warning(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.install()
        original = Pipeline.stage
        def stage(pipeline, name, *args, **kwargs):
            if name == 'review':
                raise ToolError('保存的复核记录无效。', 'cache_mismatch')
            return original(pipeline, name, *args, **kwargs)
        with mock.patch.object(Pipeline, 'stage', stage):
            self.assertEqual(self.run_v5(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')
        self.assertFalse(any(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_router_propagates_unexpected_local_preparation_error(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        data = {'sentences': [{'id': '0', 'text': 'Cache.'}], 'context': [], 'glossary': []}

        @contextmanager
        def invalid_preparation(*args):
            raise ToolError('Invalid saved data.', 'cache_mismatch')
            yield

        with mock.patch.object(Pipeline, '_reserve_quota', invalid_preparation), \
             mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
            with self.assertRaises(ToolError) as error:
                Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                    'draft', data, ids=['0'], originals=['Cache.'])
        self.assertEqual(error.exception.code, 'cache_mismatch')

    def test_smaller_candidate_budget_falls_back_to_a_larger_candidate(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [
            {'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}}, {'id': 'qiniu_paid'}]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        sentence = 'Synthetic sentence. ' * 200
        data = {'sentences': [{'id': '0', 'text': sentence}], 'context': [], 'glossary': []}
        calls = []

        class Transport:
            def request(self, method, url, **kwargs):
                kwargs['on_dispatch']()
                calls.append(kwargs['json']['model'])
                body = {'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'translations': {'0': '译文'}})}}]}
                return HTTPReply(200, json.dumps(body, ensure_ascii=False).encode('utf-8'))

        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()), \
             mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
            _, origin, _ = Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                'draft', data, ids=['0'], originals=[sentence])
        self.assertEqual(origin, 'qiniu_paid')
        self.assertEqual(calls, ['deepseek/deepseek-v4.1-flash'])

    def test_oversized_single_sentence_uses_reliable_word_boundaries(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}}]
        self.config.settings.mt_workers = 1
        raw = timed_raw([f'cache{i:03}' for i in range(400)], word_ms=20)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertGreater(len(drafts), 1)
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.assertEqual(''.join(item['text'] for draft in drafts for item in draft['sentences']),
                         raw['transcripts'][0]['sentences'][0]['text'])

    def test_deny_paid_budget_never_depends_on_larger_paid_model(self):
        self.config.settings.mt_backends = [
            {'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}},
            {'id': 'qiniu_paid', 'capabilities': {'input_bytes': 32768}}]
        self.config.settings.mt_workers = 1
        raw = timed_raw([f'cache{i:03}' for i in range(400)], word_ms=20)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertGreater(sum(stage == 'draft' for stage, _, _ in self.calls), 1)
        self.assertFalse(any(data['_model'] == 'deepseek/deepseek-v4.1-flash' for _, _, data in self.calls))

    def test_budget_planning_ignores_expired_or_credential_mismatched_large_candidate(self):
        for reason in ('expired', 'credential'):
            with self.subTest(reason=reason):
                from subtitle_tool.mt_runtime import _QUOTAS
                _QUOTAS.clear()
                self.config.settings.mt_backends = [
                    {'id': 'qiniu_free', 'capabilities': {'input_bytes': 32768}},
                    {'id': 'openrouter_free', 'capabilities': {'input_bytes': 3000}}]
                self.config.settings.mt_workers = 1
                prefix = 'e' if reason == 'expired' else 'c'
                raw = timed_raw([f'{prefix}cache{i:03}' for i in range(400)], word_ms=20)
                self.save_result(raw)
                Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny', overwrite=True)
                if reason == 'credential':
                    self.config.set_qiniu_key('changed-qiniu-after-freeze')
                with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=reason != 'expired'):
                    self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
                self.assertGreater(sum(stage == 'draft' for stage, _, _ in self.calls), 1)
                self.assertTrue(all(data['_model'] == 'openrouter/free' for _, _, data in self.calls))
                self.calls.clear()
                self.config.set_qiniu_key('synthetic-qiniu')

    def test_budget_planning_ignores_unverified_bailian_when_smaller_route_is_available(self):
        self.config.settings.mt_backends = [
            {'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen-test'},
            {'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}}]
        self.config.settings.mt_workers = 1
        raw = timed_raw([f'cache{i:03}' for i in range(400)], word_ms=20)
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny')
        with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
            self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertGreater(sum(stage == 'draft' for stage, _, _ in self.calls), 1)
        self.assertTrue(all(data['_model'] != 'qwen-test' for _, _, data in self.calls))

    def test_budget_planning_handles_bound_bailian_that_has_no_usable_quota(self):
        from subtitle_tool.mt_bailian_quota import VerifiedIdentity
        for condition in ('stopped', 'empty', 'expired'):
            with self.subTest(condition=condition):
                self.config.settings.mt_backends = [
                    {'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen-test'},
                    {'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}}]
                self.config.settings.mt_workers = 1
                raw = timed_raw([f'{condition}cache{i:03}' for i in range(400)], word_ms=20)
                self.save_result(raw)
                Engine(self.config, self.store).reflow(self.job_id, mt_paid='deny', overwrite=True)
                source = mock.Mock()
                source.bound_to.return_value = True
                source.query.return_value = json.dumps([{'model': 'qwen-test', 'type': 'Text',
                    'remaining': 0 if condition == 'empty' else 100000, 'total': 100000,
                    'remainingPercent': 0 if condition == 'empty' else 100,
                    'expires': '2000-01-01' if condition == 'expired' else '2099-12-31',
                    'autoStop': condition != 'stopped'}]).encode()
                with mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True), \
                     mock.patch('subtitle_tool.mt_bailian_binding.LocalBailianBinding') as factory, \
                     mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
                    binding = factory.return_value
                    binding.source = source
                    binding.identity.side_effect = lambda route: VerifiedIdentity(
                        'synthetic-account', 'offline-space', route.backend.key_fingerprint, 'cn-beijing', 'c' * 64)
                    binding.protection.return_value = True
                    self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
                self.assertGreater(sum(stage == 'draft' for stage, _, _ in self.calls), 1)
                self.assertTrue(all('maas.aliyuncs.com' not in url for _, url, _ in self.calls))
                self.assertGreater(source.query.call_count, 0)
                self.calls.clear()

    def test_json_object_and_schema_candidates_share_expiration_priority(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [
            {'id': 'first', 'provider': 'bailian', 'base_url': None, 'model': 'qwen3.8-27b',
             'credential_ref': 'bailian', 'tier': 'free', 'weight': 1, 'rpm': 0,
             'capabilities': {'response_format': 'json_object', 'input_bytes': 32768, 'output_tokens': 4096}},
            {'id': 'second', 'provider': 'bailian', 'base_url': None, 'model': 'qwen3.8-max-0902',
             'credential_ref': 'bailian', 'tier': 'free', 'weight': 1, 'rpm': 0,
             'capabilities': {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 4096}}]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        pipeline = Pipeline(engine, self.job_id, manifest, scope, 1)
        pipeline.bailian_quota.priority = lambda candidate: date(2099, 12, 20 if candidate.id == 'first' else 10)
        result = pipeline.router.run(lambda candidate, timeout, attempt: candidate.id)
        self.assertEqual(result.entry.id, 'second')

    def test_cached_large_bailian_stage_is_checked_before_quota_or_smaller_network_budget(self):
        from subtitle_tool.mt_bailian_quota import BailianQuota, VerifiedIdentity
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [
            {'id': 'bailian_paid', 'tier': 'free', 'model': 'qwen-test'},
            {'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}}]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        source = mock.Mock()
        source.bound_to.return_value = True
        source.query.return_value = json.dumps([{'model': 'qwen-test', 'type': 'Text', 'remaining': 100000,
            'total': 100000, 'remainingPercent': 100, 'expires': '2099-12-31', 'autoStop': True}]).encode()
        def quota(control, **kwargs):
            kwargs.update(source=source, protection=lambda route, row: True, observation=lambda proof, rows: None,
                          identity=lambda route: VerifiedIdentity('synthetic-account', 'offline-space',
                                                                 route.backend.key_fingerprint, 'cn-beijing', 'c' * 64))
            return BailianQuota(control, **kwargs)
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                kwargs['on_dispatch']()
                calls.append(url)
                body = {'model': 'qwen-test', 'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
                        'choices': [{'finish_reason': 'stop', 'message': {
                            'content': json.dumps({'translations': {'0': '缓存译文'}})}}]}
                return HTTPReply(200, json.dumps(body).encode())
        text = 'synthetic sentence ' * 300
        data = {'sentences': [{'id': '0', 'text': text, 'asr_language': 'en',
                               'source_language': 'English', 'decision_origin': 'automatic'}],
                'context': [], 'glossary': []}
        with mock.patch.dict('subtitle_tool.mt_bailian_quota._ACCOUNTS', {}, clear=True), \
             mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()), \
             mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
            with mock.patch('subtitle_tool.mt_bailian_quota.BailianQuota', side_effect=quota):
                _, first_origin, _ = Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                    'draft', data, ids=['0'], originals=[text])
            with self.store.db:
                self.store.db.execute('DELETE FROM mt_stages WHERE job_id=?', (self.job_id,))
            _, cached_origin, _ = Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                'draft', data, ids=['0'], originals=[text])
        self.assertEqual((first_origin, cached_origin), ('bailian_paid', 'bailian_paid'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(source.query.call_count, 1)

    def test_retime_accepts_saved_route_origins_without_network(self):
        self.install()
        self.assertEqual(self.run_v5(), 'completed')
        self.calls.clear()
        engine = Engine(self.config, self.store)
        engine.retime(self.job_id, overwrite=True)
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(self.calls, [])

    def test_retime_rejects_tampered_final_production_link(self):
        self.install()
        self.assertEqual(self.run_v5(), 'completed')
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
        self.assertEqual(self.run_v5(), 'completed')
        cue = self.store.cues(self.job_id)[0]
        with self.store.db:
            self.store.db.execute('UPDATE cues SET translated=? WHERE job_id=? AND cue_id=?',
                                  ('被修改的译文', self.job_id, cue['cue_id']))
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'cache_mismatch')

    def test_retime_requires_used_origins_but_not_unused_candidate_key(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}, {'id': 'bailian_paid'}]
        self.install()
        with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=False):
            self.assertEqual(self.run_v5(), 'completed')
        self.assertTrue(all(cue['mt_origin'] == 'bailian_paid' for cue in self.store.cues(self.job_id)))
        self.config.set_qiniu_key('changed-unused-qiniu')
        Engine(self.config, self.store).retime(self.job_id, overwrite=True)

    def test_retime_rejects_changed_used_key_before_saving_or_publishing(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.install()
        self.assertEqual(self.run_v5(), 'completed')
        before = self.store.job(self.job_id)
        output = Path(before['output'])
        content = output.read_bytes()
        self.config.set_qiniu_key('changed-used-qiniu')
        self.calls.clear()
        with self.assertRaises(ToolError) as error:
            Engine(self.config, self.store).retime(self.job_id, overwrite=True)
        self.assertEqual(error.exception.code, 'mt_account_changed')
        self.assertEqual(self.store.job(self.job_id), before)
        self.assertEqual(output.read_bytes(), content)
        self.assertFalse((self.config.root / '.build' / 'retime').exists())
        self.assertEqual(self.calls, [])

    def test_already_published_file_recovers_without_original_mt_key(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.install()
        self.assertEqual(self.run_v5(), 'completed')
        self.store.update(self.job_id, 'failed', error='synthetic interruption', code='local_io')
        self.config.set_qiniu_key('changed-after-publish')
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])

    def test_unpublished_translation_still_requires_used_mt_key(self):
        self.config.settings.mt_backends = [{'id': 'qiniu_free'}]
        self.install()
        self.assertEqual(self.run_v5(), 'completed')
        output = Path(self.store.job(self.job_id)['output'])
        output.unlink()
        self.store.update(self.job_id, 'failed', error='synthetic interruption', code='local_io')
        self.config.set_qiniu_key('changed-before-republish')
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'mt_account_changed')

    def test_resume_with_a_previously_committed_group_validates_route_origin(self):
        self.config.settings.mt_workers = 1
        self.save_result(result(*[(f'Technical sentence {i}.', 'en') for i in range(9)]))
        Engine(self.config, self.store).reflow(self.job_id)
        self.fail_second_group = True
        self.assertEqual(self.run_v5(), 'failed')
        self.assertTrue(any(cue['translated'] for cue in self.store.cues(self.job_id)))
        self.fail_second_group = False
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertTrue(all(cue['translated'] for cue in self.store.cues(self.job_id)))

    def test_tampered_saved_stage_metadata_blocks_reuse(self):
        self.install()
        self.fail_stage = 'align'
        self.assertEqual(self.run_v5(), 'failed')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'draft')
        row['record']['stage'] = 'review'
        row['record']['request_model'] = 'invalid-synthetic-model'
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(row['record']), self.job_id, row['key']))
        self.fail_stage = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')

    def test_non_string_actual_model_cannot_be_accepted_from_saved_stage(self):
        self.install()
        self.fail_stage = 'align'
        self.assertEqual(self.run_v5(), 'failed')
        row = next(item for item in self.store.mt_stages(self.job_id) if item['stage'] == 'draft')
        row['record']['actual_model'] = False
        with self.store.db:
            self.store.db.execute('UPDATE mt_stages SET record=? WHERE job_id=? AND stage_key=?',
                                  (json.dumps(row['record']), self.job_id, row['key']))
        self.fail_stage = None
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_v5(), 'failed')
        self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_mismatch')

    def test_openrouter_observed_exhaustion_falls_back_without_free_post(self):
        self.config.settings.mt_backends = [{'id': 'openrouter_free'}, {'id': 'bailian_paid'}]
        self.quota_remaining = 0
        self.install()
        self.assertEqual(self.run_v5(), 'completed', self.store.job(self.job_id)['error'])
        self.assertGreater(self.quota_queries, 0)
        self.assertFalse(any('openrouter.ai' in url for _, url, _ in self.calls))
        self.assertTrue(all(cue['mt_origin'] == 'bailian_paid' for cue in self.store.cues(self.job_id)))

    def test_denied_paid_still_reuses_exact_saved_stage_before_trimming_context(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        self.config.settings.mt_backends = [
            {'id': 'qiniu_free', 'capabilities': {'input_bytes': 3000}}, {'id': 'qiniu_paid'}]
        self.install()
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
            with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=False):
                _, origin, _ = Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                    'draft', data, ids=['0'], originals=['Cache.'])
            self.assertEqual(origin, 'qiniu_paid')
            manifest['mt_route_snapshot']['routing']['paid_enabled'] = False
            self.store.update(self.job_id, manifest=manifest)
            with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
                _, origin, _ = Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                    'draft', data, ids=['0'], originals=['Cache.'])
            with self.store.db:
                self.store.db.execute('DELETE FROM mt_stages WHERE job_id=?', (self.job_id,))
            with mock.patch('subtitle_tool.mt_routing.qiniu_free_active', return_value=True):
                _, cached_origin, _ = Pipeline(engine, self.job_id, manifest, scope, 1).stage(
                    'draft', data, ids=['0'], originals=['Cache.'])
        self.assertEqual(origin, 'qiniu_paid')
        self.assertEqual(cached_origin, 'qiniu_paid')
        self.assertEqual(calls, ['deepseek/deepseek-v4.1-flash'])

    def test_explicit_bailian_arrearage_blocks_same_account_paid_routes_only(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        capabilities = {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 4096}
        def entry(identity, model, tier):
            return {'id': identity, 'provider': 'bailian', 'base_url': None, 'model': model,
                    'credential_ref': 'bailian', 'tier': tier, 'weight': 1, 'rpm': 0,
                    'capabilities': capabilities}
        self.config.settings.mt_backends = [entry('free_model', 'synthetic-free', 'free'),
                                            entry('paid_one', 'synthetic-paid-one', 'paid'),
                                            entry('paid_two', 'synthetic-paid-two', 'paid')]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                if kwargs.get('on_dispatch'):
                    kwargs['on_dispatch']()
                model = kwargs['json']['model']
                calls.append(model)
                if model == 'synthetic-free':
                    return HTTPReply(403, b'{"error":{"code":"Unclassified"}}')
                if model == 'synthetic-paid-one':
                    return HTTPReply(failure_status, b'{"error":{"code":"Arrearage"}}')
                body = {'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'translations': {'0': '完整译文'}})}}]}
                return HTTPReply(200, json.dumps(body).encode('utf-8'))
        # This routing test assumes quota admission already succeeded; separate
        # regressions exercise the real, fail-closed Bailian admission path.
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()), \
             mock.patch.object(Pipeline, '_reserve_quota', lambda pipeline, entry, wait, stage, data:
                               pipeline.quota.reserve(entry, wait)):
            for failure_status in (400, 403):
                with self.subTest(status=failure_status):
                    calls.clear()
                    pipeline = Pipeline(engine, self.job_id, manifest, scope, 1)
                    for text in ['First synthetic sentence.', 'Second synthetic sentence.']:
                        data = {'sentences': [{'id': '0', 'text': text}], 'context': [], 'glossary': []}
                        with self.assertRaises(ToolError) as error:
                            pipeline.stage('draft', data, ids=['0'], originals=[text])
                        self.assertEqual(error.exception.code, 'mt_route_exhausted')
                    self.assertEqual(calls, ['synthetic-free', 'synthetic-paid-one', 'synthetic-free'])

    def test_free_tier_only_response_disables_exact_model_even_when_labelled_paid(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        capabilities = {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 4096}
        def entry(identity, model):
            return {'id': identity, 'provider': 'bailian', 'base_url': None, 'model': model,
                    'credential_ref': 'bailian', 'tier': 'paid', 'weight': 1, 'rpm': 0,
                    'capabilities': capabilities}
        self.config.settings.mt_backends = [entry('paid_one', 'synthetic-paid-one'),
                                            entry('paid_two', 'synthetic-paid-two')]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                kwargs['on_dispatch']()
                model = kwargs['json']['model']
                calls.append(model)
                if model == 'synthetic-paid-one':
                    return HTTPReply(403, b'{"error":{"code":"AllocationQuota.FreeTierOnly"}}')
                body = {'model': model, 'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'translations': {'0': '完整译文'}})}}]}
                return HTTPReply(200, json.dumps(body).encode())
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()):
            pipeline = Pipeline(engine, self.job_id, manifest, scope, 1)
            for text in ('First synthetic sentence.', 'Second synthetic sentence.'):
                data = {'sentences': [{'id': '0', 'text': text}], 'context': [], 'glossary': []}
                _, origin, _ = pipeline.stage('draft', data, ids=['0'], originals=[text])
                self.assertEqual(origin, 'paid_two')
        self.assertEqual(calls, ['synthetic-paid-one', 'synthetic-paid-two', 'synthetic-paid-two'])

    def test_unclassified_403_does_not_disable_other_paid_models(self):
        from subtitle_tool.mt_pipeline_v5 import Pipeline
        capabilities = {'response_format': 'json_schema', 'input_bytes': 32768, 'output_tokens': 4096}
        def entry(identity, model):
            return {'id': identity, 'provider': 'bailian', 'base_url': None, 'model': model,
                    'credential_ref': 'bailian', 'tier': 'paid', 'weight': 1, 'rpm': 0,
                    'capabilities': capabilities}
        self.config.settings.mt_backends = [entry('paid_one', 'synthetic-paid-one'),
                                            entry('paid_two', 'synthetic-paid-two')]
        self.install()
        manifest = self.store.job(self.job_id)['manifest']
        engine = Engine(self.config, self.store)
        _, scope = engine.identity(self.job_id, manifest, persist=False)
        calls = []
        class Transport:
            def request(self, method, url, **kwargs):
                if kwargs.get('on_dispatch'):
                    kwargs['on_dispatch']()
                model = kwargs['json']['model']
                calls.append(model)
                if model == 'synthetic-paid-one':
                    return HTTPReply(403, b'{"error":{"code":"Unclassified"}}')
                body = {'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'translations': {'0': '完整译文'}})}}]}
                return HTTPReply(200, json.dumps(body).encode('utf-8'))
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()):
            pipeline = Pipeline(engine, self.job_id, manifest, scope, 1)
            data = {'sentences': [{'id': '0', 'text': 'Synthetic sentence.'}], 'context': [], 'glossary': []}
            _, origin, _ = pipeline.stage('draft', data, ids=['0'], originals=['Synthetic sentence.'])
        self.assertEqual(origin, 'paid_two')
        self.assertEqual(calls, ['synthetic-paid-one', 'synthetic-paid-two'])

    def test_manual_language_change_does_not_reuse_previous_language_cache(self):
        from subtitle_tool.review import ReviewDecision
        self.install()
        engine = Engine(self.config, self.store)
        self.assertEqual(self.run_v5(engine=engine), 'completed')
        self.calls.clear()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['overwrite'] = True
        self.store.update(self.job_id, 'needs_review', manifest, code='language_uncertain')
        engine.apply_review(self.job_id, self.store.cues(self.job_id)[0]['cue_id'],
                            ReviewDecision('translate', 'Japanese'))
        engine.finish_review(self.job_id)
        self.assertEqual(self.run_v5(engine=engine), 'completed', self.store.job(self.job_id)['error'])
        drafts = [data for stage, _, data in self.calls if stage == 'draft']
        self.assertTrue(drafts)
        self.assertTrue(any(item['source_language'] == 'Japanese' and item['decision_origin'] == 'manual'
                            for data in drafts for item in data['sentences']))
