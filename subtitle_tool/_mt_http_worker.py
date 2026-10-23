"""Private networking child. No task imports, database, retries, logs or files."""
from __future__ import annotations

import base64
import json
import signal
import sys

import requests


MAX_RESPONSE = 4 * 1024 * 1024


def main():
    # The parent owns Ctrl+C and kills this process at its monotonic deadline.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        request = json.loads(sys.stdin.buffer.read(128 * 1024))
        with requests.request(request['method'], request['url'], headers=request['headers'],
                              json=request.get('json'), timeout=(15, 90),
                              allow_redirects=False, stream=True) as response:
            body = bytearray()
            for chunk in response.iter_content(8192):
                if len(body) + len(chunk) > MAX_RESPONSE:
                    return {'error': 'response_too_large'}
                body.extend(chunk)
            return {'status': response.status_code, 'body': base64.b64encode(body).decode('ascii'),
                    'retry_after': response.headers.get('Retry-After', '')[:128]}
    except requests.RequestException:
        return {'error': 'network'}
    except Exception:
        return {'error': 'transport'}


if __name__ == '__main__':
    sys.stdout.buffer.write(json.dumps(main(), separators=(',', ':')).encode('ascii'))
    sys.stdout.buffer.flush()
