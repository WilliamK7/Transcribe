"""Perform one HTTP request in an isolated child process."""
from __future__ import annotations

import base64
import json
import signal
import sys

import requests


MAX_RESPONSE = 4 * 1024 * 1024


class ResponseTooLarge(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate stream field')
        result[key] = value
    return result


def _stream_events(chunks):
    """Decode bounded SSE frames, including chunked UTF-8 and CRLF boundaries."""
    buffer, data, size = bytearray(), [], 0
    for chunk in chunks:
        size += len(chunk)
        if size > MAX_RESPONSE:
            raise ResponseTooLarge()
        buffer.extend(chunk)
        while (end := buffer.find(b'\n')) >= 0:
            line = bytes(buffer[:end]).removesuffix(b'\r')
            del buffer[:end + 1]
            if not line:
                if data:
                    yield b'\n'.join(data).decode('utf-8')
                    data = []
            elif line.startswith(b'data:'):
                data.append(line[5:].removeprefix(b' '))
            elif line == b'data':
                data.append(b'')
            # Comments and SSE event/id/retry fields are not model content.
    # EOF is never equivalent to the protocol's completion marker.
    raise ValueError('Stream ended before DONE')


def gather_chat_stream(chunks):
    """Return a whole Chat response only after a terminal choice and DONE.

    Reasoning deltas are not part of the answer. Their bytes still count against
    the response cap, and the final reported usage is kept for accounting.
    """
    model, content, refusal, finish, usage = None, [], [], None, None
    for event in _stream_events(chunks):
        if event == '[DONE]':
            if finish is None or model is None:
                raise ValueError('Incomplete stream')
            message = {'content': ''.join(content)}
            if refusal:
                message['refusal'] = ''.join(refusal)
            result = {'model': model, 'choices': [{'finish_reason': finish, 'message': message}]}
            if usage is not None:
                result['usage'] = usage
            return result
        body = json.loads(event, object_pairs_hook=_unique_object)
        if not isinstance(body, dict):
            raise ValueError('Invalid stream object')
        if body.get('error') is not None:
            return {'error': body['error']}
        if 'model' in body:
            identity = body['model']
            if not isinstance(identity, str) or not identity or model is not None and model != identity:
                raise ValueError('Inconsistent stream model')
            model = identity
        if body.get('usage') is not None:
            usage = body['usage']
        choices = body.get('choices')
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError('Invalid stream choices')
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get('index')) is not int or choice['index'] != 0:
            raise ValueError('Invalid stream choice')
        if choice.get('error') is not None:
            return {'error': choice['error']}
        delta = choice.get('delta')
        if not isinstance(delta, dict) or delta.get('role', 'assistant') != 'assistant':
            raise ValueError('Invalid stream delta')
        if finish is not None:
            # OpenRouter's usage trailer repeats the empty terminal choice.
            # It may report accounting, never append content or change finish.
            if (body.get('usage') is not None and choice.get('finish_reason') == finish and
                    all(key == 'role' or value is None or value == '' for key, value in delta.items())):
                continue
            raise ValueError('Content after stream finish')
        for key, target in [('content', content), ('refusal', refusal)]:
            text = delta.get(key)
            if text is not None:
                if not isinstance(text, str):
                    raise ValueError('Invalid stream text')
                target.append(text)
        if delta.get('tool_calls') or delta.get('function_call'):
            raise ValueError('Unexpected stream tool call')
        finish = choice.get('finish_reason')
        if finish is not None and (not isinstance(finish, str) or not finish):
            raise ValueError('Invalid stream finish')
    raise ValueError('Incomplete stream')


def main():
    # The parent owns Ctrl+C and kills this process at its monotonic deadline.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        request = json.loads(sys.stdin.buffer.read(128 * 1024))
        # magpie may queue and try other members before any response bytes.
        # The parent enforces the full-response deadline and cancellation;
        # retain a socket bound as well if the parent exits unexpectedly.
        timeout = request['timeout']
        with requests.request(request['method'], request['url'], headers=request['headers'],
                              json=request.get('json'), timeout=(min(15, timeout), timeout),
                              allow_redirects=False, stream=True) as response:
            content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            chunks = response.iter_content(8192)
            if response.status_code == 200 and content_type == 'text/event-stream':
                body = json.dumps(gather_chat_stream(chunks), ensure_ascii=False).encode('utf-8')
                if len(body) > MAX_RESPONSE:
                    raise ResponseTooLarge()
            else:
                body = bytearray()
                for chunk in chunks:
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        raise ResponseTooLarge()
                    body.extend(chunk)
            identity = response.headers.get('x-vsr-selected-model')
            headers = ({'x-vsr-selected-model': identity} if isinstance(identity, str) and
                       len(identity) <= 160 and '\r' not in identity and '\n' not in identity else {})
            return {'status': response.status_code, 'body': base64.b64encode(body).decode('ascii'), 'headers': headers}
    except ResponseTooLarge:
        return {'error': 'response_too_large'}
    except requests.RequestException:
        return {'error': 'network'}
    except Exception:
        return {'error': 'transport'}


if __name__ == '__main__':
    sys.stdout.buffer.write(json.dumps(main(), separators=(',', ':')).encode('ascii'))
    sys.stdout.buffer.flush()
