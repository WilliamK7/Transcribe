"""Shared synthetic transport and deterministic routing fixtures."""
import json
from unittest import mock

from subtitle_tool.engine import Engine
from subtitle_tool.mt_quality import PROMPTS
from subtitle_tool.mt_transport import HTTPReply
from tests.fixtures import SavedResultFixture, result, timed_raw


class PoolPipelineFixture(SavedResultFixture):
    def setUp(self):
        super().setUp()
        self.config.settings.mt_routing = None
        self.config.settings.mt_workers = 2
        self.calls = []
        self.review_failure = False
        self.review_issue = False
        self.dismiss_review = False
        self.no_change_correction = False
        self.fail_stage = None
        self.cancel_after = None
        self.fail_second_group = False
        self.stage_hook = None
        self.glossary_terms = lambda data: []
        self.corrected_translations = None
        self.actual_model = lambda stage, request: {'glossary': 'review-model'}.get(stage, stage + '-model')

    def run_pool(self, cancel=None, engine=None, run=None):
        parent = self
        class Transport:
            def request(self, method, url, **kwargs):
                if method != 'POST':
                    raise AssertionError('Translation must use the gateway POST endpoint')
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
                if stage == 'translate' and parent.fail_second_group and any(
                        'sentence 8' in item['text'] for item in data['sentences']):
                    return HTTPReply(400, b'{}')
                if stage == 'review' and parent.review_failure:
                    return HTTPReply(400, b'{}')
                if stage == 'glossary':
                    value = {'terms': parent.glossary_terms(data)}
                elif stage == 'translate':
                    value = {'translations': {item['id']: '完整句译文。' for item in data['fragments']}}
                elif stage == 'judge':
                    suffix = (str(sum(call[0] == 'judge' for call in parent.calls))
                              if not parent.no_change_correction else '')
                    value = {'translations': (dict(data['translations']) if parent.no_change_correction
                             else {item['id']: '字幕完整译文。' + suffix for item in data['fragments']})}
                    if parent.corrected_translations is not None:
                        value = {'translations': parent.corrected_translations(data)}
                else:
                    value = {'issues': ([{'id': '0', 'category': 'number', 'evidence': '数字有疑点'}]
                                        if parent.review_issue else [])}
                if stage == 'judge':
                    value['issues'] = [] if parent.dismiss_review else data['issues']
                content = json.dumps(value, ensure_ascii=False)
                response = {'model': parent.actual_model(stage, payload['model']), 'usage': {'prompt_tokens': 10, 'completion_tokens': 4},
                            'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
                if stage == parent.cancel_after and cancel is not None:
                    cancel.set()
                return HTTPReply(200, json.dumps(response, ensure_ascii=False).encode('utf-8'))
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport', return_value=Transport()):
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
        self.assertEqual(manifest['translation_strategy_version'], 6)
