"""Synthetic SSE responses exercise the real isolated HTTP worker."""
import json
import threading
import unittest

from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_quality import BatchValidationError, parse_stage_response
from subtitle_tool.mt_transport import ProcessTransport, TransportError
from tests.test_mt_runtime import ChildProcesses, LocalServer


def event(value, *, newline='\n'):
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ('data: ' + data + newline + newline).encode('utf-8')


def part(text=None, *, finish=None, model='synthetic/reviewer', **fields):
    return {'model': model, 'choices': [{'index': 0, 'delta': {'content': text, **fields}, 'finish_reason': finish}]}


def clean_stream():
    return [b': keepalive\r\n\r\n', event(part(role='assistant')),
            event(part(reasoning_content='private reasoning')),
            event(part('{"issues":')), event(part('[]}', finish='stop')),
            event({'model': 'synthetic/reviewer', 'choices': [], 'usage': {
                'prompt_tokens': 23, 'completion_tokens': 17, 'completion_tokens_details': {'reasoning_tokens': 12}}}),
            event('[DONE]')]


class StreamWorkerTests(unittest.TestCase):
    def setUp(self):
        from subtitle_tool import _mt_http_worker as worker
        self.worker = worker

    def gather(self, chunks):
        return self.worker.gather_chat_stream(chunks)

    def test_split_utf8_crlf_keepalive_usage_and_reasoning_are_assembled(self):
        chunks = [event(part('{"translations":{"0":"电')), event(part('压。"}}', finish='stop')),
                  *clean_stream()[-2:]]
        wire = b': alive\r\n\r\n' + b''.join(chunks).replace(b'\n', b'\r\n')
        result = self.gather(wire[i:i+1] for i in range(len(wire)))
        parsed = parse_stage_response('translate', result, ids=['0'], originals=['Voltage.'])
        self.assertEqual(parsed, {'translations': {'0': '电压。'}})
        self.assertEqual(result['usage']['completion_tokens_details']['reasoning_tokens'], 12)
        self.assertNotIn('private reasoning', json.dumps(self.gather(clean_stream())))

    def test_done_finishes_without_waiting_for_connection_close(self):
        def chunks():
            yield from clean_stream()
            raise AssertionError('Read after DONE')
        self.assertEqual(parse_stage_response('review', self.gather(chunks()), ids=['0']), {'issues': []})

    def test_partial_json_never_becomes_a_completed_response(self):
        broken = [clean_stream()[:-1], [event(part('{"issues":[]}')), event('[DONE]')],
                  [event(part('{"issues":[]}', finish='stop'))],
                  [event(part('{"issues":[]}', finish='stop')), event(part('extra')), event('[DONE]')],
                  [event(part('{"issues":')), event(part('[]}', model='another/reviewer', finish='stop')), event('[DONE]')],
                  [event({'model': 'synthetic/reviewer', 'choices': [
                      {'index': 1, 'delta': {'content': '{"issues":[]}'}, 'finish_reason': 'stop'}]}), event('[DONE]')],
                  [b'data: {"choices":[],"choices":[]}\n\n', event('[DONE]')]]
        for chunks in broken:
            with self.subTest(chunks=len(chunks)), self.assertRaises(ValueError):
                self.gather(chunks)

    def test_truncation_and_refusal_reach_existing_validation(self):
        response = self.gather([event(part('{"issues":[]}', finish='length')), event('[DONE]')])
        with self.assertRaises(BatchValidationError) as caught:
            parse_stage_response('review', response, ids=['0'])
        self.assertEqual(caught.exception.category, 'output_truncated')
        response = self.gather([event(part(finish='stop', refusal='private refusal')), event('[DONE]')])
        from subtitle_tool.mt_runtime import service_failure
        self.assertEqual(service_failure(200, response), 'content_policy_violation')

    def test_stream_error_discards_partial_answer_and_preserves_classification(self):
        from subtitle_tool.mt_runtime import service_failure
        response = self.gather([event(part('{"issues":[]}', finish='stop')),
                               event({'error': {'metadata': {'error_type': 'timeout'}, 'message': 'private error'}})])
        self.assertEqual(service_failure(200, response), 'timeout')
        self.assertNotIn('choices', response)

    def test_length_error_keeps_terminal_metadata_without_accepting_partial_content(self):
        from subtitle_tool.mt_runtime import service_failure
        usage = {'prompt_tokens': 30, 'completion_tokens': 4096,
                 'completion_tokens_details': {'reasoning_tokens': 4096}}
        terminal = {**part('', finish='length'), 'usage': usage,
                    'error': {'metadata': {'error_type': 'max_tokens_exceeded'}}}
        for placement in ('top', 'choice'):
            frame = json.loads(json.dumps(terminal))
            if placement == 'choice':
                frame['choices'][0]['error'] = frame.pop('error')
            with self.subTest(placement=placement):
                response = self.gather([event(part('private partial output', refusal='')),
                                       event(frame), event('[DONE]')])
                self.assertEqual(service_failure(200, response), 'output_truncated')
                self.assertEqual(response['model'], 'synthetic/reviewer')
                self.assertEqual(response['usage'], usage)
                self.assertNotIn('private partial output', json.dumps(response))
                with self.assertRaises(BatchValidationError):
                    parse_stage_response('review', response, ids=['0'])

    def test_earlier_refusal_survives_terminal_errors_without_private_text(self):
        from subtitle_tool.mt_runtime import service_failure
        error = {'metadata': {'error_type': 'max_tokens_exceeded'}}
        frames = [{'error': error}]
        for finish in ('length', 'error'):
            frames.append({**part('', finish=finish), 'error': error})
            frame = part('', finish=finish)
            frame['choices'][0]['error'] = error
            frames.append(frame)
        for frame in frames:
            with self.subTest(frame=frame):
                response = self.gather([event(part('private partial output', refusal='private refusal')),
                                       event(frame), event('[DONE]')])
                self.assertEqual(service_failure(200, response), 'content_policy_violation')
                self.assertIs(response['choices'][0]['message']['refusal'], True)
                self.assertNotIn('private', json.dumps(response))

    def test_multiline_data_and_absent_usage_remain_valid_without_inventing_counts(self):
        wire = b'event: message\ndata: {"model":"synthetic/reviewer",\ndata: "choices":['
        wire += b'{"index":0,"delta":{"content":"{\\"issues\\":[]}"},"finish_reason":"stop"}]}\n\n'
        response = self.gather([wire, event('[DONE]')])
        self.assertEqual(parse_stage_response('review', response, ids=['0']), {'issues': []})
        self.assertNotIn('usage', response)

    def test_openrouter_final_usage_can_repeat_an_empty_finished_choice(self):
        trailer = {**part('', role='assistant', finish='stop'),
                   'usage': {'prompt_tokens': 8, 'completion_tokens': 4}}
        response = self.gather([event(part('{"issues":[]}', finish='stop')), event(trailer), event('[DONE]')])
        self.assertEqual(parse_stage_response('review', response, ids=['0']), {'issues': []})
        self.assertEqual(response['usage'], trailer['usage'])
        for bad in [part('more', finish='stop'), part('', finish='length'), part('', finish='stop', tool_calls=[{}]),
                    part('', finish='stop', reasoning_content='more')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.gather([event(part('{"issues":[]}', finish='stop')),
                             event({**bad, 'usage': trailer['usage']}), event('[DONE]')])


class StreamProcessTests(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer()
        self.addCleanup(self.server.close)
        self.server.headers = {'Content-Type': 'text/event-stream; charset=utf-8',
                               'X-Vsr-Selected-Model': 'synthetic/reviewer'}
        self.control = MTControl(threading.Event())
        self.children = self.enterContext(ChildProcesses())

    def send(self, path='/ok', timeout=5):
        return ProcessTransport().request('POST', self.server.url + path, headers={},
            json={'stream': True, 'stream_options': {'include_usage': True}}, control=self.control, timeout=timeout)

    def test_real_worker_returns_one_validated_body_and_model_header(self):
        self.server.body = b''.join(clean_stream())
        reply = self.send()
        self.assertEqual(reply.status, 200)
        body = json.loads(reply.body)
        self.assertEqual(parse_stage_response('review', body, ids=['0']), {'issues': []})
        self.assertEqual(body['model'], 'synthetic/reviewer')
        self.assertEqual(body['usage']['completion_tokens'], 17)
        self.assertEqual(reply.headers, {'x-vsr-selected-model': 'synthetic/reviewer'})
        self.assertNotIn('private', reply.body.decode())
        self.assertEqual(self.children.active_count, 0)

    def test_incomplete_stream_is_a_safe_transport_error(self):
        self.server.body = b''.join(clean_stream()[:-1])
        with self.assertRaises(TransportError) as caught:
            self.send()
        self.assertEqual(caught.exception.category, 'transport')
        self.assertEqual(self.children.active_count, 0)

    def test_real_worker_keeps_length_error_evidence_for_fallback(self):
        from subtitle_tool.mt_runtime import service_failure
        terminal = {**part('', finish='length'),
                    'error': {'metadata': {'error_type': 'max_tokens_exceeded'}},
                    'usage': {'prompt_tokens': 30, 'completion_tokens': 4096}}
        self.server.body = b''.join([event(part('private partial output')), event(terminal), event('[DONE]')])
        body = json.loads(self.send().body)
        self.assertEqual(service_failure(200, body), 'output_truncated')
        self.assertEqual(body['usage'], terminal['usage'])
        self.assertNotIn('private partial output', json.dumps(body))
        self.assertEqual(self.children.active_count, 0)

    def test_size_cap_counts_discarded_reasoning_and_keepalives(self):
        self.server.body = b': ' + b'x' * (4 * 1024 * 1024) + b'\n\n' + b''.join(clean_stream())
        with self.assertRaises(TransportError) as caught:
            self.send()
        self.assertEqual(caught.exception.category, 'response_too_large')
        self.assertEqual(self.children.active_count, 0)

    def test_json_reply_is_still_validated_when_gateway_returns_one_body(self):
        self.server.headers['Content-Type'] = 'application/json'
        self.server.body = b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"issues\\":[]}"}}]}'
        reply = self.send()
        self.assertEqual(parse_stage_response('review', json.loads(reply.body), ids=['0']), {'issues': []})
