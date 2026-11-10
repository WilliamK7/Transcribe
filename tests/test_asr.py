"""Offline checks for direct ASR transport and storage credential separation."""
from __future__ import annotations

import threading
from unittest import mock

import requests

from subtitle_tool.api import Bailian
from subtitle_tool.common import ASR_MODEL, SubmissionUnknown, ToolError
from tests.fixtures import TemporaryTest, response


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
        self.assertEqual(submit['json'], {'model': ASR_MODEL, 'input': {'file_url': 'oss://synthetic/audio.flac'},
                                         'parameters': {'channel_id': [0], 'enable_words': True, 'enable_itn': False}})

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
