"""Offline checks for native ASR forwarding through the resident magpie gateway."""
from __future__ import annotations

import threading
from unittest import mock

import requests

from subtitle_tool.api import Bailian
from subtitle_tool.common import ASR_MODEL, SubmissionUnknown, ToolError
from tests.fixtures import TemporaryTest, response


GATEWAY = {
    'base_url': 'http://127.0.0.1:3425/v1',
    'asr_provider': 'transcribe-bailian',
    'upload_provider': 'transcribe-upload',
    'key': 'synthetic-gateway-key',
}
POLICY = {
    'policy': 'synthetic-policy', 'signature': 'synthetic-signature',
    'upload_dir': 'synthetic/audio', 'upload_host': 'https://storage.example.test',
    'oss_access_key_id': 'synthetic-access', 'x_oss_object_acl': 'private',
    'x_oss_forbid_overwrite': 'true',
}


class MagpieASRTests(TemporaryTest):
    def cloud(self, gateway=GATEWAY):
        return Bailian('synthetic-upstream-key', 'beijing', threading.Event(),
                       workspace_id='offline-space', gateway=gateway)

    def test_policy_submit_query_use_native_routes_and_only_gateway_credentials(self):
        cloud = self.cloud()
        replies = [response({'data': POLICY}),
                   response({'output': {'task_id': 'task/a'}, 'request_id': 'synthetic-request'}),
                   response({'output': {'task_status': 'SUCCEEDED'}})]
        with mock.patch('requests.request', side_effect=replies) as request:
            self.assertEqual(cloud.policy(), POLICY)
            self.assertEqual(cloud.submit('oss://synthetic/audio.flac'), ('task/a', 'synthetic-request'))
            self.assertEqual(cloud.query('task/a')['output']['task_status'], 'SUCCEEDED')
        self.assertEqual([call.args[:2] for call in request.call_args_list], [
            ('GET', GATEWAY['base_url'] + '/magpie/native/transcribe-upload/api/v1/uploads'),
            ('POST', GATEWAY['base_url'] + '/magpie/native/transcribe-bailian/api/v1/services/audio/asr/transcription'),
            ('GET', GATEWAY['base_url'] + '/magpie/native/transcribe-bailian/api/v1/tasks/task%2Fa'),
        ])
        for call in request.call_args_list:
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer ' + GATEWAY['key'])
            self.assertEqual(call.kwargs['headers']['User-Agent'], 'Transcribe/1')
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertNotIn('synthetic-upstream-key', str(call))
        self.assertEqual(request.call_args_list[0].kwargs['params'], {'action': 'getPolicy', 'model': ASR_MODEL})
        submit = request.call_args_list[1].kwargs
        self.assertEqual(submit['headers']['X-DashScope-Async'], 'enable')
        self.assertEqual(submit['headers']['X-DashScope-OssResourceResolve'], 'enable')
        self.assertEqual(submit['json'], {'model': ASR_MODEL, 'input': {'file_url': 'oss://synthetic/audio.flac'},
                                         'parameters': {'channel_id': [0], 'enable_words': True, 'enable_itn': False}})

    def test_gateway_base_normalization_preserves_prefix(self):
        cloud = self.cloud(dict(GATEWAY, base_url='https://LOCALHOST:443/prefix/v1///'))
        self.assertEqual(cloud.base, 'https://localhost/prefix/v1/magpie/native/transcribe-bailian')

    def test_invalid_gateway_is_rejected_without_network_or_configuration_values(self):
        cases = [False, [], 'private-value', {}, {**GATEWAY, 'extra': 'private-value'}]
        cases += [{key: value for key, value in GATEWAY.items() if key != missing} for missing in GATEWAY]
        cases += [dict(GATEWAY, **{field: value})
                  for field in ('asr_provider', 'upload_provider')
                  for value in (None, 3, '', '.', '..', 'private/value', 'private%2fvalue',
                                'private value', 'private\nvalue', 'x' * 65)]
        cases += [dict(GATEWAY, base_url=value) for value in
                  (None, '', 'http://private.example/v1', 'https://private-user:private-pass@example.test/v1',
                   'https://example.test/v1?private-token=value', 'https://example.test/v1/chat/completions')]
        cases += [dict(GATEWAY, key=value) for value in (None, 3, '', 'private value', 'private\nvalue', 'private\x00value')]
        with mock.patch('requests.request') as request:
            for index, gateway in enumerate(cases):
                with self.subTest(case=index):
                    with self.assertRaises(ToolError) as error:
                        self.cloud(gateway)
                    self.assertNotIn('private', str(error.exception))
                    self.assertNotIn('synthetic-', str(error.exception))
            request.assert_not_called()

    def test_submit_uncertainty_never_retries_the_gateway(self):
        cases = [requests.Timeout('private exception'), response({}, 502), response({}),
                 response({'output': []}), response({'output': {'task_id': ''}})]
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
        self.assertNotIn(GATEWAY['key'], str(upload.call_args))
        self.assertNotIn(GATEWAY['key'], str(request.call_args))

    def test_omitted_gateway_keeps_direct_workspace_contract(self):
        cloud = Bailian('synthetic-upstream-key', 'beijing', threading.Event(), workspace_id='offline-space')
        self.assertEqual(cloud.base, 'https://offline-space.cn-beijing.maas.aliyuncs.com')
        with mock.patch('requests.request', return_value=response({'data': POLICY})) as request:
            cloud.policy()
        self.assertEqual(request.call_args.args, ('GET', 'https://dashscope.aliyuncs.com/api/v1/uploads'))
        self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-upstream-key')
