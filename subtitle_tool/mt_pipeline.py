"""Private, resumable glossary/draft/alignment/review translation pipeline."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import threading
from pathlib import Path
from types import SimpleNamespace

from .api import retry_after_seconds
from .common import MAX_LAYOUT_ROUNDS, Cancelled, ToolError, check_cancel
from .mt_backend import saved_mt
from .mt_diagnostics import MTDiagnostics, batch_response_metadata, safe_model_id
from .mt_pool import saved_route
from .mt_quality import (BatchValidationError, QUALITY_PROMPT_VERSION, make_stage_payload, parse_stage_response, plan_sentence_groups,
                         stage_cache_key, validate_glossary, validate_stage_value)
from .mt_routing import CacheHit, MTRouter, RouteEntry, RouteFailure
from .mt_runtime import bailian_failure, service_failure
from .mt_transport import TransportError
from .review import decision_origin
from .subtitles import (layout_warnings, plan_display_timing, plan_fragments, revise_layout, source_context, split_mt_units, validate_fragment,
                        validate_unit_coverage)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


class GroupProgress:
    """Report active group stages without exposing source text or model output."""
    interval = 15.0

    def __init__(self, engine, job_id):
        self.engine, self.job_id = engine, job_id
        self.lock = threading.Lock()
        self.active = {}
        self.last = 0.0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join()

    def _report(self, now):
        if not self.active:
            return
        counts = {}
        for name, _ in self.active.values():
            counts[name] = counts.get(name, 0) + 1
        elapsed = int(max(now - started for _, started in self.active.values()))
        self.engine.progress(self.job_id, 'mt_v5_progress', {'stages': counts, 'elapsed': elapsed}, None)
        self.last = now

    def set(self, group, stage):
        with self.lock:
            now = time.monotonic()
            if group not in self.active or self.active[group][0] != stage:
                self.active[group] = (stage, now)
            if now - self.last >= 5:
                self._report(now)

    def finish(self, group):
        with self.lock:
            self.active.pop(group, None)

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                self._report(time.monotonic())


class GlossaryPrefix:
    """Freeze each group's terminology from its source-ordered prefix."""
    def __init__(self, pipeline, count, sources):
        self.pipeline, self.sources = pipeline, sources
        self.condition = threading.Condition()
        self.results, self.prefixes = [None] * count, [None] * count
        self.next = 0
        self.terms, self.seen = [], set()
        self._add(validate_glossary([], sources, pipeline.snapshot['quality']['manual_glossary']))

    def _add(self, terms):
        # Manual entries are added first; among model entries the first source
        # group wins, independently of the order in which workers finish.
        for term in terms:
            identity = term['source'].casefold()
            if identity not in self.seen:
                self.terms.append(term)
                self.seen.add(identity)

    def for_group(self, index, extracted):
        with self.condition:
            self.results[index] = extracted
            while self.next < len(self.results) and self.results[self.next] is not None:
                self._add(validate_glossary(self.results[self.next], self.sources, {}))
                self.prefixes[self.next] = tuple(self.terms)
                self.next += 1
            self.condition.notify_all()
            while self.prefixes[index] is None:
                self.pipeline.engine.mt_control.check_start()
                if self.pipeline.group_progress is not None:
                    self.pipeline.group_progress.set(index, 'waiting')
                self.condition.wait(.1)
            self.pipeline.engine.mt_control.check_start()
            return self.prefixes[index]


def _valid_usage(value):
    return (value is None or isinstance(value, dict) and set(value) == {'input_tokens', 'output_tokens'} and
            all(type(item) is int and item >= 0 for item in value.values()))


def _sentence(unit, identity):
    source = unit['source']
    return {'id': identity, 'text': unit['text'], 'asr_language': source.get('language', ''),
            'source_language': source.get('source_lang', 'auto'), 'decision_origin': decision_origin(source)}


def check_pool(config, identity, route, cancel):
    """Online smoke test using an in-memory task and one synthetic sentence."""
    from .mt_control import MTControl
    from .store import Store
    from .policy import current_execution_snapshot
    primary = next(entry['backend'] for entry in route['entries'] if entry['backend'] is not None)
    from dataclasses import asdict
    manifest = {**identity, 'mt_snapshot': primary, 'mt_route_snapshot': route,
                'processing_version': 2, 'subtitle_version': 2, 'display_timing_version': 2,
                'subtitle_settings': {k: asdict(config.settings)[k] for k in
                                      ['zh_line_width', 'other_line_width', 'chinese_first']},
                'translation_strategy_version': 5, 'execution_snapshot': current_execution_snapshot()}
    scope = {**identity, 'processing_version': 2}
    store = Store(Path(':memory:'))
    try:
        now = time.time()
        job_id = '0' * 32
        with store.db:
            store.db.execute('INSERT INTO jobs(id,source,output,output_key,state,manifest,created,updated) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             (job_id, '', '', '', 'queued', json.dumps(manifest), now, now))
        engine = SimpleNamespace(config=config, store=store, mt_control=MTControl(cancel))
        pipeline = Pipeline(engine, job_id, manifest, scope)
        sentence = 'A cache stores a previously computed result.'
        data = {'sentences': [{'id': '0', 'text': sentence, 'asr_language': 'en',
                              'source_language': 'English', 'decision_origin': 'automatic'}],
                'context': [], 'glossary': []}
        _, origin, _ = pipeline.stage('draft', data, ids=['0'], originals=[sentence])
        entry = pipeline.profiles[origin]
        return {'status': 'ok', 'backend': origin, 'provider': entry['backend']['provider'],
                'model': entry['backend']['model']}
    finally:
        store.close()


def validate_provenance(engine, job_id, manifest, *, verify_credentials=True):
    """Verify each saved cue is linked to its actual production stage and credential."""
    route = saved_route(manifest)
    profiles = {entry['id']: entry for entry in route['entries']}
    checked, finals = set(), {}
    try:
        for cue in engine.store.cues(job_id):
            if not cue.get('translated'):
                if cue.get('mt_origin') is not None or cue.get('mt_stage_key') is not None:
                    raise ValueError()
                continue
            origin, key = cue.get('mt_origin'), cue.get('mt_stage_key')
            if (not isinstance(origin, str) or origin not in profiles or profiles[origin]['backend'] is None or
                    not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}', key) is None):
                raise ValueError()
            if verify_credentials and origin not in checked:
                saved_mt({**manifest, 'mt_snapshot': profiles[origin]['backend']}).credential(engine.config)
                checked.add(origin)
            if key not in finals:
                saved = engine.store.mt_stage_entry(job_id, key)
                if not isinstance(saved, dict) or saved['stage'] != 'final' or not isinstance(saved['record'], dict):
                    raise ValueError()
                final = saved['record']
                required = {'backend', 'request_model', 'actual_model', 'production_stage_key', 'draft_stage_key',
                            'review_stage_keys', 'correction_stage_keys', 'cue_ids', 'value', 'warning', 'stage'}
                if (set(final) != required or final['stage'] != 'final' or type(final['warning']) is not bool or
                        not isinstance(final['actual_model'], str) or
                        safe_model_id(final['actual_model']) != final['actual_model'] or
                        not isinstance(final['cue_ids'], list) or not final['cue_ids'] or
                        any(not isinstance(identity, str) for identity in final['cue_ids']) or
                        len(set(final['cue_ids'])) != len(final['cue_ids']) or
                        not isinstance(final['value'], dict) or set(final['value']) != {'translations'} or
                        not isinstance(final['value']['translations'], dict) or
                        set(final['value']['translations']) != {str(i) for i in range(len(final['cue_ids']))}):
                    raise ValueError()
                proof = engine.store.mt_stage_entry(job_id, final['production_stage_key'])
                if (not isinstance(proof, dict) or proof['stage'] not in {'align', 'correct'} or
                        not isinstance(proof['record'], dict) or proof['record'].get('stage') != proof['stage'] or
                        proof['record'].get('backend') != final['backend'] or
                        proof['record'].get('request_model') != final['request_model'] or
                        proof['record'].get('actual_model') != final['actual_model'] or
                        proof['record'].get('value') != final['value']):
                    raise ValueError()
                for stage_name, keys in [('draft', [final['draft_stage_key']]),
                                         ('review', final['review_stage_keys']),
                                         ('correct', final['correction_stage_keys'])]:
                    if not isinstance(keys, list):
                        raise ValueError()
                    for stage_key in keys:
                        entry = engine.store.mt_stage_entry(job_id, stage_key)
                        if (not isinstance(entry, dict) or entry['stage'] != stage_name or
                                not isinstance(entry['record'], dict) or entry['record'].get('stage') != stage_name):
                            raise ValueError()
                finals[key] = final
            final = finals[key]
            if (final['backend'] != origin or final['request_model'] != profiles[origin]['backend']['model'] or
                    cue['cue_id'] not in final['cue_ids'] or
                    final['value']['translations'][str(final['cue_ids'].index(cue['cue_id']))] != cue['translated']):
                raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ToolError('译文阶段来源与保存的翻译池不一致。', 'cache_mismatch') from None


class Pipeline:
    def __init__(self, engine, job_id, manifest, scope):
        from .policy import load_policy
        self.engine, self.store, self.job_id = engine, engine.store, job_id
        load_policy(manifest)
        self.scope = dict(scope)
        self.context = threading.local()
        self.group_progress = None
        self.diagnostics = MTDiagnostics(engine.config.root, job_id)
        self.quota_warning_lock = threading.Lock()
        self.quota_warning_sent = False
        from . import mt_transport
        from .mt_bailian_quota import BailianQuota
        from .mt_bailian_binding import LocalBailianBinding
        from .mt_quota import OpenRouterQuota
        self.transport = mt_transport.ProcessTransport()
        self.quota = OpenRouterQuota(engine.mt_control, lambda entry: entry.backend.credential(engine.config),
                             transport=self.transport, on_event=self._quota_event)
        # The saved, explicitly user-confirmed binding is rechecked against the
        # current credentials and CLI login context before every admission.
        binding = LocalBailianBinding(engine.config.data, get_key=engine.config.get_key,
                                      get_scope=lambda: (engine.config.settings.workspace_id,
                                                         engine.config.settings.region))
        self.bailian_quota = BailianQuota(engine.mt_control, identity=binding.identity,
                                          protection=binding.protection, source=binding.source,
                                          observation=binding.observe,
                                          on_event=self._quota_event)
        self.snapshot = saved_route(manifest)
        self.profiles = {profile['id']: profile for profile in self.snapshot['entries']}
        entries = []
        for profile in self.snapshot['entries']:
            if profile['backend'] is None:
                continue
            backend = saved_mt({**manifest, 'mt_snapshot': profile['backend'], 'translation_strategy_version': 5})
            entries.append(RouteEntry(profile['id'], backend, profile['tier'], profile['weight'], profile['rpm']))
        routing = self.snapshot['routing']
        self.router = MTRouter(entries, paid_enabled=routing['paid_enabled'], control=engine.mt_control,
                               free_wait_seconds=routing['free_wait_seconds'], priority=self._priority,
                               on_event=self._event)

    def _event(self, event):
        event = dict(event)
        if event.get('kind') == 'route_wait' and isinstance(event.get('seconds'), (int, float)):
            event['planned_wait_ms'] = max(0, round(event['seconds'] * 1000))
        self.diagnostics.write_route({**event, 'stage': getattr(self.context, 'stage', ''),
                                      'group_key': getattr(self.context, 'group_key', ''),
                                      'run_id': getattr(self.engine, 'run_id', '')})
        if event.get('kind') == 'route_switch':
            self.store.count(self.job_id, 'mt_route_switch')
        elif event.get('kind') == 'route_failure':
            self.store.count(self.job_id, 'mt_v5_' + getattr(self.context, 'stage', 'unknown') + '_failure')

    def _quota_event(self, event):
        if event.get('kind') == 'quota_query':
            self.store.count(self.job_id, 'mt_quota_query')
        else:
            self.diagnostics.write_quota(event)
            if event.get('category') == 'quota_console_session_invalid':
                with self.quota_warning_lock:
                    progress = getattr(self.engine, 'progress', None)
                    if not self.quota_warning_sent and callable(progress):
                        self.quota_warning_sent = True
                        progress(self.job_id, 'mt_quota_login', None, None)

    def _priority(self, entry):
        if entry.tier == 'free' and entry.backend.provider == 'bailian':
            capabilities = self.profiles[entry.id]['capabilities']
            expires = self.bailian_quota.priority(entry)
            response_mode = 'json' if capabilities['response_format'] in {'json_object', 'json_schema'} else 'text'
            group = ('bailian', response_mode, capabilities['input_bytes'],
                     capabilities['output_tokens'])
            return group, expires.toordinal() if expires is not None else None
        return None

    def _reserve_quota(self, entry, wait_budget, stage, data):
        if entry.tier == 'free' and entry.backend.provider == 'bailian':
            from .mt_bailian_quota import estimate_tokens
            payload = self._request_payload(stage, data, entry)
            return self.bailian_quota.reserve(entry, wait_budget, estimate=estimate_tokens(payload))
        return self.quota.reserve(entry, wait_budget)

    def _request_payload(self, stage, data, entry):
        try:
            return make_stage_payload(stage, data, entry.backend,
                                      capabilities=self.profiles[entry.id]['capabilities'])
        except ToolError as error:
            if error.code != 'mt_input_budget':
                raise
            raise RouteFailure('local_error') from None

    def _fit(self, stage, data, *, allow_guarded_fallback=True):
        """Drop far context first, then excess glossary; never truncate targets."""
        data = copy.deepcopy(data)
        candidates, guarded, cache_only = [], [], []
        for entry in self.router.entries:
            if entry.tier == 'paid' and not self.router.paid_enabled:
                continue
            if not self.router._available(entry):
                continue
            try:
                entry.backend.credential(self.engine.config)
            except ToolError:
                continue
            if entry.tier == 'free' and entry.backend.provider == 'bailian':
                (guarded if self.bailian_quota.potential(entry) else cache_only).append(entry)
            else:
                candidates.append(entry)
        # If no model can currently be admitted, retain cache-only profiles so
        # historical exact stage results can still be looked up and recovered.
        # A bound free Bailian model can still have an unknown/expired/empty
        # quota when a group is planned. Size to another available provider
        # first, so a later refusal does not strand an otherwise usable route.
        fallback = guarded if candidates else []
        candidates = candidates or guarded or cache_only
        while True:
            fits = False
            for entry in candidates:
                try:
                    make_stage_payload(stage, data, entry.backend, capabilities=self.profiles[entry.id]['capabilities'])
                    fits = True
                    break
                except ToolError as error:
                    if error.code != 'mt_input_budget':
                        raise
            if fits:
                return data
            if data.get('context'):
                data['context'].pop()
            elif data.get('glossary'):
                data['glossary'].pop()
            else:
                if allow_guarded_fallback:
                    for entry in fallback:
                        try:
                            make_stage_payload(stage, data, entry.backend,
                                               capabilities=self.profiles[entry.id]['capabilities'])
                            return data
                        except ToolError as error:
                            if error.code != 'mt_input_budget':
                                raise
                raise ToolError('翻译阶段完整请求超过所有可用模型的输入预算。', 'mt_input_budget')

    def _work_key(self, name, data):
        return _digest({'strategy': 5, 'prompt': QUALITY_PROMPT_VERSION, 'stage': name, 'data': data,
                        'scope': self.scope, 'profiles': self.snapshot['entries']})

    def stage(self, name, data, *, ids=(), originals=(), prefer=None):
        self.engine.mt_control.check_start()
        group = getattr(self.context, 'group', None)
        if self.group_progress is not None and group is not None:
            self.group_progress.set(group, name)
        started = time.monotonic()
        data = copy.deepcopy(data)
        work_key = self._work_key(name, data)
        self.context.stage, self.context.group_key = name, work_key
        def saved_stage():
            try:
                stored = self.store.mt_stage_entry(self.job_id, work_key)
            except (ValueError, TypeError):
                raise ToolError('翻译阶段保存记录无效。', 'cache_mismatch') from None
            if stored is None:
                return None
            saved = stored['record']
            usage = saved.get('usage') if isinstance(saved, dict) else None
            if (stored['stage'] != name or not isinstance(saved, dict) or
                    set(saved) != {'value', 'backend', 'request_model', 'actual_model', 'usage', 'stage'} or
                    saved.get('stage') != name or not isinstance(saved.get('backend'), str) or
                    saved.get('backend') not in self.profiles or
                    self.profiles[saved['backend']]['backend'] is None or
                    saved.get('request_model') != self.profiles[saved['backend']]['backend']['model'] or
                    not isinstance(saved.get('actual_model'), str) or
                    safe_model_id(saved['actual_model']) != saved['actual_model'] or
                    not _valid_usage(usage)):
                raise ToolError('翻译阶段保存记录无效。', 'cache_mismatch')
            entry = next((entry for entry in self.router.entries if entry.id == saved['backend']), None)
            if entry is not None:
                try:
                    entry.backend.credential(self.engine.config)
                except ToolError:
                    pass
                else:
                    try:
                        value = validate_stage_value(name, saved.get('value'), ids=ids, originals=originals)
                    except ToolError:
                        raise ToolError('保存的翻译阶段记录无效。', 'cache_mismatch') from None
                    return value, saved['backend'], {'key': work_key, 'actual_model': saved['actual_model'],
                                                     'request_model': saved['request_model']}
            return None

        saved = saved_stage()
        if saved is not None:
            return saved

        def cached(entry):
            try:
                entry.backend.credential(self.engine.config)
                key = stage_cache_key(name, data, entry.backend, capabilities=self.profiles[entry.id]['capabilities'],
                                      scope=self.scope)
            except ToolError as error:
                if error.code in {'mt_input_budget', 'credentials', 'mt_account_changed'}:
                    return None
                raise
            raw = self.store.cache_get(key)
            if raw is None:
                return None
            try:
                record = json.loads(raw)
                if (not isinstance(record, dict) or set(record) != {'value', 'actual_model', 'usage'} or
                        not isinstance(record['actual_model'], str) or
                        safe_model_id(record['actual_model']) != record['actual_model'] or
                        not _valid_usage(record['usage'])):
                    raise ValueError()
                value = validate_stage_value(name, record['value'], ids=ids, originals=originals)
                return CacheHit({'value': value, 'actual_model': record['actual_model'], 'usage': None})
            except (ValueError, TypeError, KeyError, ToolError):
                raise ToolError('翻译阶段缓存无效，未复用。', 'cache_mismatch') from None

        def invoke(entry, timeout, attempt, reservation):
            self.engine.mt_control.check_start()
            try:
                key = entry.backend.credential(self.engine.config)
            except ToolError:
                raise RouteFailure('authentication', scope='credential', disable=True) from None
            payload = self._request_payload(name, data, entry)
            def dispatch():
                reservation.mark_sent()
                self.store.count(self.job_id, 'mt')
                self.store.count(self.job_id, 'mt_v5_' + entry.id + '_' + name + '_request')
            try:
                request_started = time.monotonic()
                reply = self.transport.request('POST', entry.backend.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                    json=payload, control=self.engine.mt_control, timeout=timeout,
                    on_dispatch=dispatch)
            except TransportError as error:
                raise RouteFailure(error.category, scope='endpoint' if error.category == 'network' else 'entry',
                                   retryable=error.retryable) from None
            try:
                body = json.loads(reply.body)
            except (ValueError, UnicodeError, RecursionError):
                body = None
            status = reply.status
            raw_usage = body.get('usage') if isinstance(body, dict) else None
            if (isinstance(raw_usage, dict) and type(raw_usage.get('prompt_tokens')) is int and
                    type(raw_usage.get('completion_tokens')) is int and raw_usage['prompt_tokens'] >= 0 and
                    raw_usage['completion_tokens'] >= 0):
                usage = {'input_tokens': raw_usage['prompt_tokens'], 'output_tokens': raw_usage['completion_tokens']}
                if entry.tier == 'free' and entry.backend.provider == 'bailian':
                    reservation.settle(usage)
            else:
                usage = None
            self.diagnostics.write_route({'kind': 'route_reply', 'stage': name, 'entry': entry.id,
                'model': entry.backend.model, 'status': status, 'duration_ms': round((time.monotonic() - request_started) * 1000),
                'group_key': work_key, 'run_id': getattr(self.engine, 'run_id', '')})
            if 200 <= status < 300:
                self.store.count(self.job_id, 'mt_success')
            if status == 429:
                self.store.count(self.job_id, 'mt_429')
            failure = (bailian_failure(status, body) if entry.backend.provider == 'bailian' else
                       service_failure(status, body, free_router=entry.backend.free_router))
            if failure is not None:
                category, scope, disable = failure.category, 'entry', False
                if status == 401 or category == 'authentication':
                    category, scope, disable = 'authentication', 'credential', True
                elif (entry.backend.provider == 'bailian' and category == 'quota_exhausted' and
                      failure.quota_source == 'free_tier_only'):
                    scope, disable = 'model_quota', True
                elif entry.tier == 'paid' and (status == 402 or category == 'payment_required'):
                    category, scope, disable = 'payment_required', 'paid_account', True
                elif status == 404 and not entry.backend.free_router:
                    scope, disable = 'model', True
                raise RouteFailure(category, scope=scope, retryable=failure.retryable,
                                   retry_after=retry_after_seconds(reply.retry_after), disable=disable) from None
            try:
                value = parse_stage_response(name, body, ids=ids, originals=originals)
            except BatchValidationError as error:
                self.store.count(self.job_id, 'mt_invalid_response')
                self.diagnostics.write({'kind': 'invalid_response', 'category': error.category,
                    'stage': name, 'model': entry.backend.model, 'request_version': 5,
                    'group_key': work_key, 'run_id': getattr(self.engine, 'run_id', ''),
                    'expected_count': len(ids), 'actual_count': error.actual_count,
                    **batch_response_metadata(body)})
                raise RouteFailure('format_exhausted') from None
            if usage is None:
                self.store.count(self.job_id, 'mt_usage_unknown')
            else:
                self.store.count(self.job_id, 'mt_input_tokens', usage['input_tokens'])
                self.store.count(self.job_id, 'mt_output_tokens', usage['output_tokens'])
            return {'value': value, 'actual_model': safe_model_id(body.get('model')), 'usage': usage}

        # Preserve the exact originally requested context and glossary for both
        # free and paid cache lookup before adapting to permitted network models.
        result = self.router.cached_result(cached, prefer_other_than=prefer, deadline=started + 180.0)
        if result is None:
            data = self._fit(name, data)
            new_key = self._work_key(name, data)
            adapted = new_key != work_key
            if adapted:
                work_key = new_key
                self.context.group_key = work_key
                saved = saved_stage()
                if saved is not None:
                    return saved
            remaining = max(0.0, 180.0 - (time.monotonic() - started))
            result = self.router.run(invoke, cache=cached if adapted else None, prefer_other_than=prefer,
                                     prepare=lambda entry, wait: self._reserve_quota(entry, wait, name, data),
                                     deadline_seconds=remaining, stage=name)
        self.diagnostics.write_route({'kind': 'route_complete', 'stage': name, 'entry': result.entry.id,
            'model': result.entry.backend.model, 'actual_model': result.value['actual_model'],
            'duration_ms': round((time.monotonic() - started) * 1000), 'wait_ms': round(result.waited * 1000),
            'attempt': result.attempts, 'switches': result.switches, 'cached': result.cached,
            'group_key': work_key, 'run_id': getattr(self.engine, 'run_id', '')})
        if result.waited:
            self.store.count(self.job_id, 'mt_route_wait_ms', round(result.waited * 1000))
        record = {**result.value, 'backend': result.entry.id, 'request_model': result.entry.backend.model,
                  'stage': name}
        cache_key = stage_cache_key(name, data, result.entry.backend,
                                    capabilities=self.profiles[result.entry.id]['capabilities'], scope=self.scope)
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_inflight()
            self.store.commit_mt_stage(self.job_id, work_key, name, record,
                cache_key=None if result.cached else cache_key,
                cache_value=None if result.cached else json.dumps(result.value, ensure_ascii=False),
                counts={'mt_v5_' + name + ('_cache_hit' if result.cached else '_completed'): 1})
        return record['value'], record['backend'], {'key': work_key, 'actual_model': record['actual_model'],
                                                    'request_model': record['request_model']}

    def _fits_group(self, units, *, allow_guarded_fallback=False):
        if units[-1]['cues'][-1]['end_ms'] - units[0]['cues'][0]['begin_ms'] > 60000:
            return False
        sentences = [_sentence(unit, str(i)) for i, unit in enumerate(units)]
        fragments = [{'id': str(n), 'sentence': str(i), 'text': cue['text']}
                     for n, (i, cue) in enumerate((i, cue) for i, unit in enumerate(units) for cue in unit['cues'])]
        base = {'sentences': sentences, 'draft': {item['id']: '译文' for item in sentences},
                'fragments': fragments, 'glossary': []}
        samples = [('glossary', {'sentences': sentences, 'manual_glossary': []}),
                   ('draft', {'sentences': sentences, 'context': [], 'glossary': []}),
                   ('align', base), ('review', {**base, 'translations': {f['id']: '译文' for f in fragments}})]
        try:
            for stage, data in samples:
                self._fit(stage, data, allow_guarded_fallback=allow_guarded_fallback)
            return True
        except ToolError as error:
            if error.code != 'mt_input_budget':
                raise
            return False

    def plan_groups(self, sources, cues):
        """Reduce complete-sentence groups first; oversized sentences use reliable cue word boundaries."""
        by_sentence = {}
        for cue in cues:
            by_sentence.setdefault(cue['sentence_id'], []).append(cue)
        result = []
        for source_group in plan_sentence_groups(sources):
            pending = []
            for source in source_group:
                members = sorted(by_sentence.get(source['sentence_id'], []), key=lambda item: item['char_start'])
                validate_unit_coverage(source, members)
                def make_unit(start, end):
                    first = 0 if start == 0 else members[start - 1]['char_end']
                    last = len(source['text']) if end == len(members) else members[end - 1]['char_end']
                    return {'source': source, 'text': source['text'][first:last], 'cues': members[start:end]}
                def split(start, end):
                    unit = make_unit(start, end)
                    if self._fits_group([unit]):
                        return [unit]
                    if end - start < 2 or not source.get('words'):
                        # A sentence that cannot be split may still fit the
                        # larger guarded model; admission remains fail-closed.
                        if self._fits_group([unit], allow_guarded_fallback=True):
                            return [unit]
                        raise ToolError('完整句超过模型预算且没有可用的可靠词时间边界。', 'mt_input_budget')
                    middle = (start + end) // 2
                    return split(start, middle) + split(middle, end)
                units = split(0, len(members))
                if len(units) > 1:
                    if pending:
                        result.append(pending)
                        pending = []
                    result.extend([[unit] for unit in units])
                    continue
                unit = units[0]
                if pending and not self._fits_group([*pending, unit]):
                    result.append(pending)
                    pending = []
                pending.append(unit)
            if pending:
                result.append(pending)
        return result

    def glossary(self, group):
        manual = self.snapshot['quality']['manual_glossary']
        local = [{**unit['source'], 'text': unit['text']} for unit in group]
        manual_evidence = validate_glossary([], local, manual)
        data = {'sentences': [_sentence(unit, unit['source']['sentence_id']) for unit in group],
                'manual_glossary': manual_evidence}
        # Advisory failures must remain part of this job's deterministic prefix:
        # otherwise a later retry could silently change an already saved draft's input.
        unavailable_key = _digest({'stage': 'glossary_unavailable', 'request': self._work_key('glossary', data)})
        try:
            unavailable = self.store.mt_stage_entry(self.job_id, unavailable_key)
        except (TypeError, ValueError):
            raise ToolError('翻译阶段保存记录无效。', 'cache_mismatch') from None
        if unavailable is not None:
            if unavailable != {'stage': 'glossary_unavailable', 'record': {'stage': 'glossary_unavailable'}}:
                raise ToolError('翻译阶段保存记录无效。', 'cache_mismatch')
            return []
        try:
            result, _, _ = self.stage('glossary', data)
            return result['terms']
        except Cancelled:
            raise
        except ToolError as error:
            self.engine.mt_control.check_start()
            if error.code not in {'mt_route_exhausted', 'mt_route_deadline'}:
                raise
            # Glossary extraction is advisory; translations still use evidenced manual entries.
            with self.engine.mt_control.lock:
                self.engine.mt_control.check_start()
                self.store.commit_mt_stage(self.job_id, unavailable_key, 'glossary_unavailable',
                                           {'stage': 'glossary_unavailable'},
                                           counts={'mt_glossary_unavailable': 1})
            return []

    def group(self, group, glossary, sources):
        self.engine.mt_control.check_start()
        members = [cue for unit in group for cue in unit['cues']]
        if not members or all(cue['translated'] for cue in members):
            return
        positions = {source['sentence_id']: index for index, source in enumerate(sources)}
        context = source_context(sources, positions[group[0]['source']['sentence_id']])
        relevant = [term for term in glossary if any(term['source'].casefold() in unit['text'].casefold() for unit in group)]
        relevant.sort(key=lambda term: term['origin'] != 'manual')
        sentences = [_sentence(unit, str(i)) for i, unit in enumerate(group)]
        draft_data = {'sentences': sentences, 'context': context, 'glossary': relevant}
        draft, draft_backend, draft_proof = self.stage('draft', draft_data, ids=[s['id'] for s in sentences],
                                                       originals=[s['text'] for s in sentences])
        source_index = {cue['cue_id']: str(i) for i, unit in enumerate(group) for cue in unit['cues']}
        fragments = [{'id': str(i), 'sentence': source_index[cue['cue_id']], 'text': cue['text']}
                     for i, cue in enumerate(members)]
        ids, originals = [f['id'] for f in fragments], [f['text'] for f in fragments]
        data = {'sentences': sentences, 'draft': draft['translations'], 'fragments': fragments, 'glossary': relevant}
        aligned, origin, production = self.stage('align', data, ids=ids, originals=originals, prefer=draft_backend)
        current = aligned['translations']
        warning = False
        review_keys, correction_keys = [], []
        for round_number in range(self.snapshot['quality']['max_revisions'] + 1):
            self.engine.mt_control.check_start()
            review_data = {**data, 'translations': current}
            try:
                review, reviewer, review_proof = self.stage('review', review_data, ids=ids, prefer=origin)
                review_keys.append(review_proof['key'])
            except Cancelled:
                raise
            except ToolError as error:
                self.engine.mt_control.check_start()
                if error.code not in {'mt_route_exhausted', 'mt_route_deadline'}:
                    raise
                warning = True
                break
            if not review['issues']:
                break
            if round_number >= self.snapshot['quality']['max_revisions']:
                warning = True
                break
            try:
                corrected, origin, production = self.stage('correct', {**review_data, 'issues': review['issues']},
                                                           ids=ids, originals=originals, prefer=reviewer)
                correction_keys.append(production['key'])
                current = corrected['translations']
            except Cancelled:
                raise
            except ToolError as error:
                self.engine.mt_control.check_start()
                if error.code not in {'mt_route_exhausted', 'mt_route_deadline'}:
                    raise
                warning = True
                break
        final_key = _digest({'stage': 'final', 'strategy': 5, 'cues': [c['cue_id'] for c in members],
                             'data': data, 'scope': self.scope, 'profiles': self.snapshot['entries']})
        record = {'backend': origin, 'request_model': production['request_model'],
                  'actual_model': production['actual_model'], 'production_stage_key': production['key'],
                  'draft_stage_key': draft_proof['key'], 'review_stage_keys': review_keys,
                  'correction_stage_keys': correction_keys, 'cue_ids': [cue['cue_id'] for cue in members],
                  'value': {'translations': current}, 'warning': warning, 'stage': 'final'}
        values = {cue['cue_id']: current[str(i)] for i, cue in enumerate(members)}
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_inflight()
            self.store.commit_mt_stage(self.job_id, final_key, 'final', record, translations=values, origin=origin,
                                       counts={'mt_subtitle_completed': len(values),
                                               'mt_quality_warning': int(warning)})


def process_subtitles(engine, job_id, manifest, settings, scope, stage):
    sources = engine.store.sources(job_id)
    if not sources:
        raise ToolError('任务缺少完整句来源记录。', 'cache_mismatch')
    if manifest.get('layout_complete'):
        if any(cue['decision'] == 'translate' and not cue['translated'] for cue in engine.store.cues(job_id)):
            raise ToolError('已完成排版缺少完整译文。', 'cache_mismatch')
        return
    source_ids = {source['sentence_id']: source for source in sources}
    pipeline = Pipeline(engine, job_id, manifest, scope)
    if not manifest.get('layout_planned'):
        cues = []
        for source in sources:
            parts = plan_fragments(source, settings)
            if source['decision'] == 'translate':
                parts = split_mt_units(source, parts, fits=lambda cue: pipeline._fits_group(
                    [{'source': source, 'text': cue['text'], 'cues': [cue]}], allow_guarded_fallback=True))
            cues.extend(parts)
        plan_display_timing(cues)
        candidate = copy.deepcopy(manifest)
        candidate.update(layout_planned=True, layout_round=0)
        engine.commit_local(job_id, manifest, candidate, engine.store.save_layout, cues)
    while True:
        check_cancel(engine.cancel)
        stage('subtitle_translating')
        cues = engine.store.cues(job_id)
        plan_display_timing(cues)
        for cue in cues:
            if cue.get('sentence_id') not in source_ids:
                raise ToolError('字幕片段缺少完整句来源。', 'cache_mismatch')
            validate_fragment(source_ids[cue['sentence_id']], cue)
        groups = pipeline.plan_groups(sources, cues)
        pending = [index for index, group in enumerate(groups)
                   if any(not cue['translated'] for unit in group for cue in unit['cues'])]
        tasks = []
        if pending:
            # Completed earlier groups still contribute their exact saved glossary
            # to the prefix used by later unfinished groups.
            groups = groups[:pending[-1] + 1]
            prefix = GlossaryPrefix(pipeline, len(groups), sources)
            progress = GroupProgress(engine, job_id)
            pipeline.group_progress = progress
            def run_group(index, group):
                pipeline.context.group = index
                progress.set(index, 'glossary')
                try:
                    glossary = prefix.for_group(index, pipeline.glossary(group))
                    pipeline.group(group, glossary, sources)
                finally:
                    progress.finish(index)
            for index, group in enumerate(groups):
                members = [cue for unit in group for cue in unit['cues']]
                tasks.append((members, lambda index=index, group=group: run_group(index, group)))
            engine.progress(job_id, 'subtitle_translating',
                            sum(c['decision'] != 'translate' or bool(c['translated']) for c in cues), len(cues))
            progress.start()
            try:
                engine._translate_pending(job_id, tasks,
                                          done=sum(c['decision'] != 'translate' or bool(c['translated']) for c in cues),
                                          total=len(cues))
            finally:
                progress.close()
                pipeline.group_progress = None
        cues = engine.store.cues(job_id)
        revised = None
        round_number = manifest['layout_round']
        if round_number < MAX_LAYOUT_ROUNDS:
            stage('layout_adjusting', round_number + 1, MAX_LAYOUT_ROUNDS)
            revised = revise_layout(cues, source_ids, settings, layout_round=round_number + 1)
        if revised is None:
            warnings = layout_warnings(cues, settings)
            if any(item['record'].get('warning') for item in engine.store.mt_stages(job_id)):
                warnings.append('部分翻译组自动复核未通过或复核服务不可用，已保留完整译文；建议人工检查。')
            if engine.store.counts(job_id).get('mt_glossary_unavailable'):
                warnings.append('部分术语分析未完成，已保留完整译文；建议人工检查术语。')
            for warning in warnings:
                if warning not in manifest['warnings']:
                    manifest['warnings'].append(warning)
            manifest['layout_complete'] = True
            engine.store.update(job_id, manifest=manifest)
            return
        candidate = copy.deepcopy(manifest)
        candidate['layout_round'] = round_number + 1
        plan_display_timing(revised)
        engine.commit_local(job_id, manifest, candidate, engine.store.save_layout, revised)
