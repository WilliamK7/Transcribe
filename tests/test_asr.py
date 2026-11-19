"""Offline checks for direct ASR transport and storage credential separation."""
from __future__ import annotations

import copy
import json
import threading
from unittest import mock

import requests

from subtitle_tool.api import Bailian
from subtitle_tool.common import ASR_MODEL, SubmissionUnknown, ToolError, asr_contract
from subtitle_tool.engine import Engine
from subtitle_tool.review import ReviewDecision
from tests.fixtures import FakeService, PipelineFixture, SavedResultFixture, TemporaryTest, response, result, unlabeled_raw


LEGACY_MODEL = 'qwen3-asr-flash-filetrans'
LEGACY_CONTRACT = {'asr': LEGACY_MODEL, 'normalizer': 4,
                   'parameters': {'channel_id': [0], 'enable_words': True, 'enable_itn': False}}
RESULT_URL = 'https://offline-result.oss-cn-beijing.aliyuncs.com/raw.json?Signature=offline'

ASR_KEY = 'synthetic-upstream-key'
POLICY = {
    'policy': 'synthetic-policy', 'signature': 'synthetic-signature',
    'upload_dir': 'synthetic/audio', 'upload_host': 'https://storage.example.test',
    'oss_access_key_id': 'synthetic-access', 'x_oss_object_acl': 'private',
    'x_oss_forbid_overwrite': 'true',
}


class ASRTests(TemporaryTest):
    def cloud(self, key=ASR_KEY):
        return Bailian('beijing', threading.Event(),
                       workspace_id='offline-space', key=key)

    def test_policy_submit_query_use_direct_endpoints_and_asr_credentials(self):
        self.assertEqual(ASR_MODEL, 'qwen-audio-3.1-asr-flash-filetrans')
        cloud = self.cloud()
        replies = [response({'data': POLICY}),
                   response({'output': {'task_id': 'task/a'}, 'request_id': 'synthetic-request'}),
                   response({'output': {'task_status': 'SUCCEEDED'}})]
        with mock.patch('requests.request', side_effect=replies) as request:
            self.assertEqual(cloud.policy(), POLICY)
            self.assertEqual(cloud.submit('oss://synthetic/audio.flac'), ('task/a', 'synthetic-request'))
            self.assertEqual(cloud.query('task/a')['output']['task_status'], 'SUCCEEDED')
        self.assertEqual([call.args[:2] for call in request.call_args_list], [
            ('GET', 'https://dashscope.aliyuncs.com/api/v1/uploads'),
            ('POST', 'https://offline-space.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription'),
            ('GET', 'https://offline-space.cn-beijing.maas.aliyuncs.com/api/v1/tasks/task%2Fa'),
        ])
        for call in request.call_args_list:
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer ' + ASR_KEY)
            self.assertEqual(call.kwargs['headers']['User-Agent'], 'Transcribe/1')
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(request.call_args_list[0].kwargs['params'], {'action': 'getPolicy', 'model': ASR_MODEL})
        submit = request.call_args_list[1].kwargs
        self.assertEqual(submit['headers']['X-DashScope-Async'], 'enable')
        self.assertEqual(submit['headers']['X-DashScope-OssResourceResolve'], 'enable')
        self.assertEqual(submit['json'], {'model': ASR_MODEL, 'input': {'file_urls': ['oss://synthetic/audio.flac']},
                                         'parameters': {'channel_id': [0], 'keep_dialect': True,
                                                        'special_word_filter': '{"system_reserved_filter":false}'}})

    def test_invalid_key_is_rejected_without_network_or_secret(self):
        with mock.patch('requests.request') as request:
            for key in (None, 3, '', 'private value', 'private\nvalue', 'private\x00value'):
                with self.subTest(key_type=type(key).__name__), self.assertRaises(ToolError) as error:
                    self.cloud(key)
                self.assertNotIn('private', str(error.exception))
            request.assert_not_called()

    def test_submit_uncertainty_never_retries(self):
        cases = [requests.Timeout('private exception'), response({}, 502), response({}, 503),
                 response({}), response({'output': None}), response({'output': []}),
                 response({'output': {'task_id': ''}})]
        for index, outcome in enumerate(cases):
            with self.subTest(case=index):
                cloud = self.cloud()
                effect = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with mock.patch('requests.request', **effect) as request, \
                        mock.patch.object(cloud.http, 'pause') as pause:
                    with self.assertRaises(SubmissionUnknown):
                        cloud.submit('oss://synthetic/audio.flac')
                    request.assert_called_once()
                    pause.assert_not_called()

    def test_submit_explicit_rejection_is_not_retried_or_marked_unknown(self):
        for status in (400, 401, 403, 429):
            with self.subTest(status=status), \
                    mock.patch('requests.request', return_value=response({'code': 'Denied'}, status)) as request:
                with self.assertRaises(ToolError) as error:
                    self.cloud().submit('oss://synthetic/audio.flac')
                self.assertNotIsInstance(error.exception, SubmissionUnknown)
                request.assert_called_once()

    def test_object_upload_and_signed_result_download_stay_direct_without_authorization(self):
        cloud = self.cloud()
        audio = self.path / 'synthetic.flac'
        audio.write_bytes(b'synthetic audio')
        with mock.patch.object(cloud, 'policy', return_value=POLICY), \
                mock.patch('requests.post', return_value=response({})) as upload:
            self.assertTrue(cloud.upload(audio).startswith('oss://synthetic/audio/'))
        self.assertEqual(upload.call_args.args, (POLICY['upload_host'],))
        self.assertEqual(set(upload.call_args.kwargs['headers']), {'Content-Type'})
        self.assertFalse(upload.call_args.kwargs['allow_redirects'])
        signed_url = 'https://results.example.aliyuncs.com/result.json?Signature=synthetic-value'
        with mock.patch('requests.request', return_value=response({'transcripts': []})) as request:
            self.assertEqual(cloud.download(signed_url), {'transcripts': []})
        self.assertEqual(request.call_args.args, ('GET', signed_url))
        self.assertIsNone(request.call_args.kwargs['headers'])
        self.assertNotIn(ASR_KEY, str(upload.call_args))
        self.assertNotIn(ASR_KEY, str(request.call_args))


class ASRRecoveryTests(SavedResultFixture):
    def prepare_query(self):
        self.save_result(normalized=False)
        (self.cache / 'raw-0000.json').unlink()

    def test_current_query_downloads_one_successful_subtask_with_unlabeled_words(self):
        self.prepare_query()
        raw = unlabeled_raw(['欢迎', '使用', '百炼。'])
        fake = FakeService(raw, query_output={'task_status': 'SUCCEEDED', 'results': [
            {'subtask_status': 'SUCCEEDED', 'transcription_url': RESULT_URL}]})
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(json.loads((self.cache / 'raw-0000.json').read_bytes()), raw)
        source = self.store.sources(self.job_id)[0]
        self.assertEqual(source['language'], '')
        self.assertEqual(source['word_timing_status'], 'reliable')
        self.assertEqual(len(source['words']), 3)

    def test_invalid_or_failed_subtask_never_downloads_or_publishes(self):
        self.prepare_query()
        good = {'subtask_status': 'SUCCEEDED', 'transcription_url': RESULT_URL}
        cases = [({'result': {'transcription_url': RESULT_URL}}, 'response_schema'),
                 ({'results': []}, 'response_schema'),
                 ({'results': [good, good]}, 'response_schema'),
                 ({'results': [None]}, 'response_schema'),
                 ({'results': [{'transcription_url': RESULT_URL}]}, 'response_schema'),
                 ({'results': [{'subtask_status': 'RUNNING', 'transcription_url': RESULT_URL}]}, 'response_schema'),
                 ({'results': [{'subtask_status': 'SUCCEEDED'}]}, 'response_schema'),
                 ({'results': [{'subtask_status': 'FAILED', 'code': 'InvalidAudio',
                                'message': 'private sample text', 'transcription_url': RESULT_URL}]}, 'InvalidAudio')]
        for fields, code in cases:
            with self.subTest(fields=fields):
                fake = FakeService(result(), query_output={'task_status': 'SUCCEEDED', **fields})
                self.assertEqual(self.run_fake(fake), 'failed')
                job = self.store.job(self.job_id)
                self.assertEqual(job['error_code'], code)
                self.assertEqual(job['request_id'], 'offline-query')
                self.assertNotIn('private sample text', job['error'])
                self.assertEqual(len(fake.calls), 1)
                self.assertFalse((self.cache / 'raw-0000.json').exists())
                self.assertFalse(self.source.with_suffix('.srt').exists())

    def test_unknown_model_or_changed_parameters_stop_before_cloud_calls(self):
        self.save_result(normalized=False)
        original = self.store.job(self.job_id)['manifest']
        cases = [('asr', 'unrecognized-asr-model'), ('parameters', {'channel_id': [0, 1]})]
        for field, value in cases:
            with self.subTest(field=field):
                manifest = copy.deepcopy(original)
                manifest['contract'][field] = value
                self.store.update(self.job_id, manifest=manifest)
                fake = FakeService(result())
                self.assertEqual(self.run_fake(fake), 'failed')
                self.assertEqual(self.store.job(self.job_id)['error_code'], 'cache_version')
                self.assertEqual(fake.calls, [])

    def test_legacy_known_task_resumes_with_legacy_result_and_no_resubmit(self):
        self.prepare_query()
        manifest = self.store.job(self.job_id)['manifest']
        manifest['contract'] = copy.deepcopy(LEGACY_CONTRACT)
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(('保留原文。', 'zh')), query_output={
            'task_status': 'SUCCEEDED', 'result': {'transcription_url': RESULT_URL}})
        Engine(self.config, self.store).resume(self.job_id)
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(self.store.job(self.job_id)['manifest']['contract'], LEGACY_CONTRACT)
        self.assertEqual(fake.named('/asr/transcription'), [])
        self.assertEqual(len(fake.named('/tasks/offline-task')), 1)

    def test_legacy_normalized_result_and_reflow_keep_recognition_provenance(self):
        self.save_result(result(('保留原文。', 'zh')))
        manifest = self.store.job(self.job_id)['manifest']
        manifest['contract'] = copy.deepcopy(LEGACY_CONTRACT)
        self.store.update(self.job_id, manifest=manifest)
        raw = (self.cache / 'raw-0000.json').read_bytes()
        fake = FakeService(result())
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        Engine(self.config, self.store).reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(self.store.job(self.job_id)['manifest']['contract'], LEGACY_CONTRACT)
        self.assertEqual((self.cache / 'raw-0000.json').read_bytes(), raw)
        self.assertEqual(fake.calls, [])

    def test_foreign_unlabeled_words_survive_manual_language_review_and_reflow(self):
        self.prepare_query()
        # A one-token interjection remains deliberately ambiguous without an ASR label.
        raw = unlabeled_raw(['OK'], language='en')
        fake = FakeService(raw, query_output={'task_status': 'SUCCEEDED', 'results': [
            {'subtask_status': 'SUCCEEDED', 'transcription_url': RESULT_URL}]})
        self.assertEqual(self.run_fake(fake), 'needs_review')
        source = self.store.sources(self.job_id)[0]
        self.assertEqual((source['language'], source['decision']), ('', 'uncertain'))
        self.assertEqual(len(source['words']), 1)
        self.store.apply_review(self.job_id, source['cue_id'], ReviewDecision('original'))
        engine = Engine(self.config, self.store)
        engine.finish_review(self.job_id)
        self.assertEqual(self.run_fake(fake), 'completed')
        engine.reflow(self.job_id, overwrite=True)
        self.assertEqual(self.run_fake(fake), 'completed')
        self.assertEqual(self.store.sources(self.job_id)[0]['words'], source['words'])
        self.assertEqual(len(fake.calls), 3)


class ASRPipelineTests(PipelineFixture):
    def test_new_job_saves_current_model_before_any_cloud_request(self):
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest.get('contract'), asr_contract())

    def test_legacy_unsubmitted_job_uses_legacy_upload_and_submit(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest['contract'] = copy.deepcopy(LEGACY_CONTRACT)
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(('保留原文。', 'zh')), query_output={
            'task_status': 'SUCCEEDED', 'result': {'transcription_url': RESULT_URL}})
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(fake.named('/uploads')[0][2]['params']['model'], LEGACY_MODEL)
        submitted = fake.named('/asr/transcription')[0][2]['json']
        self.assertEqual(submitted['model'], LEGACY_MODEL)
        self.assertEqual(set(submitted['input']), {'file_url'})
        self.assertEqual(submitted['parameters'], LEGACY_CONTRACT['parameters'])

    def test_preupgrade_queue_without_snapshot_keeps_legacy_model(self):
        manifest = self.store.job(self.job_id)['manifest']
        manifest.pop('contract')
        self.store.update(self.job_id, manifest=manifest)
        fake = FakeService(result(('保留原文。', 'zh')), query_output={
            'task_status': 'SUCCEEDED', 'result': {'transcription_url': RESULT_URL}})
        self.assertEqual(self.run_fake(fake), 'completed', self.store.job(self.job_id)['error'])
        self.assertEqual(fake.named('/asr/transcription')[0][2]['json']['model'], LEGACY_MODEL)
        self.assertEqual(self.store.job(self.job_id)['manifest']['contract'], LEGACY_CONTRACT)
