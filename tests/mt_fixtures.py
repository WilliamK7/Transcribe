"""Shared synthetic transport and deterministic routing fixtures."""
import json
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from subtitle_tool.engine import Engine
from subtitle_tool.mt_backend import MTBackend
from subtitle_tool.mt_control import MTControl
from subtitle_tool.mt_quality import PROMPTS
from subtitle_tool.mt_routing import MTRouter, RouteEntry
from subtitle_tool.mt_transport import HTTPReply
from tests.fixtures import SavedResultFixture, result, timed_raw


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []
        self.utc = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def wait(self, control, seconds):
        control.check_start()
        self.waits.append(seconds)
        self.now += seconds


def entry(name, tier='free', weight=1, *, model=None, key='key-a', url='https://a.example/v1',
          provider='openai_compatible', rpm=0):
    return RouteEntry(name, MTBackend(provider, url, model or name, key, 5), tier,
                      weight=weight, rpm=rpm)


class RouterFixture(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.control = MTControl(threading.Event(), clock=self.clock)
        self.events = []

    def router(self, entries, *, paid_enabled=True, **kwargs):
        return MTRouter(entries, paid_enabled=paid_enabled, control=self.control, clock=self.clock,
                        utcnow=lambda: self.clock.utc, wait=self.clock.wait,
                        on_event=kwargs.pop('on_event', self.events.append), **kwargs)


class PoolPipelineFixture(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.set_qiniu_key('synthetic-qiniu')
        (self.config.data / 'mt-key.txt').write_text('synthetic-openrouter', encoding='utf-8')
        self.config.settings.mt_provider = 'pool'
        self.config.settings.mt_workers = 2
        self.calls = []
        self.review_failure = False
        self.review_issue = False
        self.no_change_correction = False
        self.fail_stage = None
        self.cancel_after = None
        self.fail_second_group = False
        self.quota_remaining = 1000
        self.quota_queries = 0
        self.stage_hook = None
        self.glossary_terms = lambda data: []
        self.corrected_translations = None

    def run_pool(self, cancel=None, engine=None, run=None):
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
                    value = {'terms': parent.glossary_terms(data)}
                elif stage == 'draft':
                    value = {'translations': {item['id']: '完整句译文。' for item in data['sentences']}}
                elif stage in {'align', 'correct'}:
                    suffix = (str(sum(call[0] == 'correct' for call in parent.calls))
                              if stage == 'correct' and not parent.no_change_correction else '')
                    value = {'translations': (dict(data['translations']) if stage == 'correct' and parent.no_change_correction
                             else {item['id']: '字幕完整译文。' + suffix for item in data['fragments']})}
                    if stage == 'correct' and parent.corrected_translations is not None:
                        value = {'translations': parent.corrected_translations(data)}
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
             mock.patch('subtitle_tool.mt_backend.qiniu_free_active', return_value=True):
            return run() if run is not None else (engine or Engine(self.config, self.store, cancel)).run(self.job_id)

    def install(self, *, split=False):
        raw = result(('A cache stores a result.', 'en'), ('The next call reuses it.', 'en'))
        if split:
            raw = timed_raw(['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'], word_ms=1000)
            raw['transcripts'][0]['sentences'].append(
                {'begin_time': 8500, 'end_time': 9400, 'text': 'A short ending.', 'language': 'en'})
        self.save_result(raw)
        Engine(self.config, self.store).reflow(self.job_id)
        manifest = self.store.job(self.job_id)['manifest']
        self.assertEqual(manifest['translation_strategy_version'], 5)
