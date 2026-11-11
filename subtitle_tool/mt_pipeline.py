"""Private, resumable window glossary and translate/review/judge pipeline."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from types import SimpleNamespace

from .common import MAX_LAYOUT_ROUNDS, Cancelled, ToolError, check_cancel
from .mt_backend import MTBackend
from .mt_context import group_context, sentence_context, target_span, trim_context, valid_context_subset
from .mt_diagnostics import MTDiagnostics, batch_response_metadata, safe_model_id
from .mt_pool import saved_route
from .mt_planning import make_unit, restore_group_plan, save_group_plan
from .mt_quality import (BatchValidationError, GLOSSARY_SCHEDULE, QUALITY_PROMPT_VERSION, correction_stalled,
                         estimate_stage_output, make_stage_payload, manual_terms, parse_stage_response, plan_sentence_groups,
                         stage_cache_key, term_appears, term_evidence, validate_stage_value, validate_judgment)
from .mt_routing import STAGE_TIMEOUT, MagpieRoute, RouteEntry, RouteExhausted, RouteFailure
from .mt_runtime import service_failure
from .mt_transport import TransportError
from .mt_identity import canonical_model, resolve_model, dynamic_model
from .review import decision_origin
from .subtitles import (layout_warnings, plan_display_timing, plan_fragments, revise_layout, split_mt_units,
                        validate_unit_coverage)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _independent_model(model, forbidden):
    return bool(model and not model.startswith('group/') and not dynamic_model(model) and
                canonical_model(model) not in forbidden)


def _record_model(record, identities):
    return resolve_model(record['actual_model'], {}, identities, saved=record.get('resolved_model'), require_admitted=True)


def _proof_model(proof):
    return proof.get('resolved_model') or canonical_model(proof['actual_model'])


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
        self.engine.progress(self.job_id, 'mt_v6_progress', {'stages': counts, 'elapsed': elapsed}, None)
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
    """Accumulate evidenced senses in source order, independent of worker completion."""
    def __init__(self, pipeline, sources):
        self.pipeline = pipeline
        self.terms, self.indices, self.manual = [], {}, set()
        by_id = {source['sentence_id']: source['text'] for source in sources}
        self._add({**term, 'evidence': term_evidence(term['source'], by_id[term['sentence']])}
                  for term in manual_terms(sources, pipeline.snapshot['quality']['manual_glossary']))

    def _add(self, terms):
        for term in terms:
            name = term['source'].casefold()
            if name in self.manual:
                continue
            if term['origin'] == 'manual':
                self.manual.add(name)
            # Distinct model translations are distinct senses. Repeated senses
            # use their latest source evidence without mutating dispatched tuples.
            identity = name, term['translation']
            if identity in self.indices:
                self.terms[self.indices[identity]] = term
            else:
                self.indices[identity] = len(self.terms)
                self.terms.append(term)

    def extend(self, extracted):
        self.pipeline.engine.mt_control.check_start()
        self._add(extracted)
        return tuple(self.terms)


def _valid_usage(value):
    return (value is None or isinstance(value, dict) and set(value) == {'input_tokens', 'output_tokens'} and
            all(type(item) is int and item >= 0 for item in value.values()))


def _sentence(unit, identity):
    source = unit['source']
    start, end = target_span(unit)
    return {'id': identity, 'text': unit['text'], 'asr_language': source.get('language', ''),
            'source_language': source.get('source_lang', 'auto'), 'decision_origin': decision_origin(source),
            'source_id': source['sentence_id'], 'char_start': start, 'char_end': end}


def _group_samples(sentences, fragments, context, glossary):
    """Stage skeletons used to select shared evidence before generating a draft."""
    base = {'sentences': sentences, 'context': context, 'glossary': glossary, 'fragments': fragments}
    review = {**base, 'translations': {item['id']: '完整译文占位文本' for item in fragments}}
    return [('translate', base), ('review', review), ('judge', {**review, 'issues': []})]



def check_pool(config, identity, route, cancel, *, magpie=None):
    """Online smoke test using an in-memory task and one synthetic sentence."""
    from .mt_control import MTControl
    from .store import Store
    from .policy import current_execution_snapshot
    from dataclasses import asdict
    manifest = {**identity, 'mt_route_snapshot': route,
                'processing_version': 2, 'subtitle_version': 2, 'display_timing_version': 2,
                'subtitle_settings': {k: asdict(config.settings)[k] for k in
                                      ['zh_line_width', 'other_line_width', 'chinese_first']},
                 'translation_strategy_version': 6, 'execution_snapshot': current_execution_snapshot(),
                 'mt_glossary_schedule': GLOSSARY_SCHEDULE}
    scope = {**identity, 'processing_version': 2}
    store = Store(Path(':memory:'))
    try:
        now = time.time()
        job_id = uuid.uuid4().hex
        with store.db:
            store.db.execute('INSERT INTO jobs(id,source,output,output_key,state,manifest,created,updated) '
                             'VALUES(?,?,?,?,?,?,?,?)',
                             (job_id, '', '', '', 'queued', json.dumps(manifest), now, now))
        engine = SimpleNamespace(config=config, store=store, mt_control=MTControl(cancel), magpie=magpie)
        pipeline = Pipeline(engine, job_id, manifest, scope)
        sentence = 'A cache stores a previously computed result.'
        data = {'sentences': [{'id': '0', 'text': sentence, 'asr_language': 'en',
                              'source_language': 'English', 'decision_origin': 'automatic'}],
                'context': [], 'glossary': [],
                'fragments': [{'id': '0', 'sentence': '0', 'text': sentence}]}
        _, origin, proof = pipeline.stage('translate', data, ids=['0'], originals=[sentence])
        entry = pipeline.profiles[origin]
        return {'status': 'ok', 'backend': origin, 'provider': entry['backend']['provider'],
                'model': entry['backend']['model'], 'actual_model': proof['actual_model'],
                'gateway_session': proof['gateway_session'], 'request_id': proof['request_id'],
                'policy_version': entry['backend']['policy_version']}
    finally:
        if 'pipeline' in locals():
            pipeline.close()
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
            if (not isinstance(origin, str) or origin not in profiles or
                    not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}', key) is None):
                raise ValueError()
            if verify_credentials and origin not in checked:
                MTBackend.from_snapshot(profiles[origin]['backend']).credential(engine.config)
                checked.add(origin)
            if key not in finals:
                saved = engine.store.mt_stage_entry(job_id, key)
                if not isinstance(saved, dict) or saved['stage'] != 'final' or not isinstance(saved['record'], dict):
                    raise ValueError()
                final = saved['record']
                required = {'backend', 'request_model', 'actual_model', 'production_stage_key', 'translation_stage_key',
                            'review_stage_keys', 'correction_stage_keys', 'cue_ids', 'value', 'warning', 'stage'}
                if (set(final) - {'resolved_model'} != required or final['stage'] != 'final' or type(final['warning']) is not bool or
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
                if (not isinstance(proof, dict) or proof['stage'] not in {'translate', 'judge'} or
                        not isinstance(proof['record'], dict) or proof['record'].get('stage') != proof['stage'] or
                        proof['record'].get('backend') != final['backend'] or
                        proof['record'].get('request_model') != final['request_model'] or
                        proof['record'].get('actual_model') != final['actual_model'] or
                        {'translations': proof['record'].get('value', {}).get('translations')} != final['value']):
                    raise ValueError()
                identities = route.get('identities', {})
                # Fixed-model stages saved before resolved_model was introduced
                # remain unchanged when a resumed run creates its new final.
                if _record_model(proof['record'], identities) != _record_model(final, identities):
                    raise ValueError()
                source_hashes = None
                if proof['stage'] == 'translate':
                    source_hashes = proof['record'].get('source_text_hashes')
                    if (final['production_stage_key'] != final['translation_stage_key'] or
                            final['correction_stage_keys'] or not isinstance(source_hashes, list) or
                            len(source_hashes) != len(final['cue_ids']) or
                            any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None
                                for value in source_hashes)):
                        raise ValueError()
                resolved_roles = {}
                for stage_name, keys in [('translate', [final['translation_stage_key']]),
                                         ('review', final['review_stage_keys']),
                                         ('judge', final['correction_stage_keys'])]:
                    if not isinstance(keys, list):
                        raise ValueError()
                    for stage_key in keys:
                        entry = engine.store.mt_stage_entry(job_id, stage_key)
                        if (not isinstance(entry, dict) or entry['stage'] != stage_name or
                                not isinstance(entry['record'], dict) or entry['record'].get('stage') != stage_name):
                            raise ValueError()
                        resolved = _record_model(entry['record'], route.get('identities', {}))
                        if dynamic_model(entry['record']['actual_model']) and stage_name != 'translate':
                            raise ValueError()
                        if resolved in resolved_roles and resolved_roles[resolved] != stage_name:
                            raise ValueError()
                        resolved_roles[resolved] = stage_name
                finals[key] = final, source_hashes
            final, source_hashes = finals[key]
            if (final['backend'] != origin or final['request_model'] != profiles[origin]['backend']['model'] or
                    cue['cue_id'] not in final['cue_ids'] or
                    final['value']['translations'][str(final['cue_ids'].index(cue['cue_id']))] != cue['translated']):
                raise ValueError()
            if (source_hashes is not None and
                    source_hashes[final['cue_ids'].index(cue['cue_id'])] != _digest(cue['text'].strip())):
                raise ValueError()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ToolError('译文阶段来源或底层型号与快照不一致；无法验证的动态旧记录须用 --reflow 从原始转写重建。',
                        'cache_mismatch') from None


class Pipeline:
    def __init__(self, engine, job_id, manifest, scope):
        from .policy import load_policy
        self.engine, self.store, self.job_id = engine, engine.store, job_id
        load_policy(manifest)
        self.scope = dict(scope)
        self.context = threading.local()
        self.group_progress = None
        self.diagnostics = MTDiagnostics(engine.config.root, job_id)
        from . import mt_transport
        self.transport = mt_transport.ProcessTransport()
        from .bailian_free import BailianFreeGuard
        self.bailian_free = BailianFreeGuard(engine.config, engine.mt_control, job_id=job_id)
        from .mt_maintenance import ChannelMaintenance
        self.channel_maintenance = ChannelMaintenance(engine.config, engine.mt_control, trial=self.bailian_free)
        self.snapshot = saved_route(manifest)
        self.identities = self.snapshot.get('identities', {})
        self.cache_scope = dict(self.scope)
        if self.snapshot['format_version'] >= 6:
            self.cache_scope['model_identity'] = _digest(self.identities)
        self.profiles = {profile['id']: profile for profile in self.snapshot['entries']}
        entries = []
        for profile in self.snapshot['entries']:
            backend = MTBackend.from_snapshot(profile['backend'])
            entries.append(RouteEntry(profile['id'], backend, profile['tier']))
        self.routers = {}
        for role, suffix in [('translate', ''), ('review', '_review'), ('judge', '_judge')]:
            selected = [entry for name in ('magpie' + suffix, 'magpie' + suffix + '_fallback')
                        for entry in entries if entry.id == name]
            self.routers[role] = MagpieRoute(selected, control=engine.mt_control, on_event=self._event)
        self.router = self.routers['translate']

    def router_for(self, stage):
        return self.routers['review' if stage == 'glossary' else stage]

    def close(self):
        self.bailian_free.stop.set()
        self.channel_maintenance.close()
        self.bailian_free.close()

    def _event(self, event):
        event = dict(event)
        if event['kind'] == 'route_attempt':
            self.context.request_id = self.context.gateway_session = None
        event['request_id'] = getattr(self.context, 'request_id', None)
        event['gateway_session'] = getattr(self.context, 'gateway_session', None)
        self.diagnostics.write_route({**event, 'stage': getattr(self.context, 'stage', ''),
                                      'group_key': getattr(self.context, 'group_key', ''),
                                      'run_id': getattr(self.engine, 'run_id', '')})
        if event.get('kind') == 'route_failure':
            self.store.count(self.job_id, 'mt_v6_' + getattr(self.context, 'stage', 'unknown') + '_failure')

    def _validate_gateway(self, entry):
        from .magpie import validate_group, validate_role_pools
        from .mt_catalog import saved_catalog
        from .mt_channel_policy import check_provider_policy
        try:
            check_provider_policy(self.engine.config, entry.backend.model, base_url=entry.backend.base_url)
        except ToolError as error:
            if error.code != 'mt_provider_excluded':
                raise
            raise RouteFailure('permission_denied', fallback_allowed=True) from None
        magpie = getattr(self.engine, 'magpie', None)
        if magpie is not None:
            magpie.ensure_ready(entry.backend.base_url, self.engine.mt_control)
        # Old fixed-model tasks can still use their commercial fallback when a
        # dynamic member was added to the live group later. Only pool validation
        # sees that new admission; response and saved-stage evidence stay frozen.
        pool_identities = {**saved_catalog(self.engine.config).get('identities', {}), **self.identities}
        if not entry.id.endswith('_fallback'):
            self.channel_maintenance.prepare(self.engine.config.settings, entry.backend.base_url)
            try:
                self.bailian_free.prepare(self.engine.config.settings, entry.backend.base_url)
                if self.bailian_free.thread is not None:
                    self.bailian_free.poll_routes()
            except Cancelled:
                raise
            except ToolError:
                raise RouteFailure('permission_denied', fallback_allowed=True) from None
        validate_role_pools(self.engine.config.settings, {
            role: [candidate.backend.model for candidate in router.entries
                   if candidate.backend.base_url == entry.backend.base_url] for role, router in self.routers.items()},
            base_url=entry.backend.base_url, identities=pool_identities)
        validate_group(self.engine.config.settings, entry.backend.model, base_url=entry.backend.base_url,
                       paid_only=entry.id.endswith('_fallback'), identities=self.identities)

    def _request_payload(self, stage, data, entry, *, repair=False):
        try:
            return make_stage_payload(stage, data, entry.backend,
                                      capabilities=self.profiles[entry.id]['capabilities'], repair=repair)
        except ToolError as error:
            if error.code != 'mt_input_budget':
                raise
            raise RouteFailure('local_error') from None

    def _invoke_stage(self, name, data, ids, originals, work_key, entry, timeout, attempt, *, forbidden_models=()):
        """One HTTP request, with content-free evidence even when validation fails."""
        self.engine.mt_control.check_start()
        started = time.monotonic()
        try:
            key = entry.backend.credential(self.engine.config)
        except ToolError:
            raise RouteFailure('authentication', scope='credential') from None
        payload = self._request_payload(name, data, entry, repair=attempt == 2)
        request_id = uuid.uuid4().hex
        session = 'transcribe-' + self.job_id + '-' + request_id
        self.context.request_id, self.context.gateway_session = request_id, session
        event = {'stage': name, 'entry': entry.id, 'model': entry.backend.model,
                 'group_key': work_key, 'run_id': getattr(self.engine, 'run_id', ''),
                 'request_id': request_id, 'gateway_session': session, 'attempt': attempt,
                 'policy_version': entry.backend.policy_version, 'dispatched': False}
        outcome = 'local_error'

        def dispatch():
            self.store.count(self.job_id, 'mt')
            self.store.count(self.job_id, 'mt_v6_' + entry.id + '_' + name + '_request')
            if entry.id.endswith('_fallback'):
                self.store.count(self.job_id, 'mt_paid_fallback_request')
            if attempt == 2:
                self.store.count(self.job_id, 'mt_format_repair_request')
            event['dispatched'] = True
            self.diagnostics.write_route({'kind': 'request_start', **event})

        try:
            try:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TransportError('deadline')
                reply = self.transport.request('POST', entry.backend.base_url + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                             'User-Agent': 'Transcribe/1', 'X-Magpie-Response-Model': 'member',
                             'X-Magpie-Session': session},
                    json=payload, control=self.engine.mt_control, timeout=remaining, on_dispatch=dispatch)
            except TransportError as error:
                outcome, event['category'] = 'transport_error', error.category
                raise RouteFailure(error.category, scope='endpoint' if error.category == 'network' else 'entry',
                                   fallback_allowed=error.category == 'deadline') from None
            try:
                body = json.loads(reply.body)
            except (ValueError, UnicodeError, RecursionError):
                body = None
            status = reply.status
            event['status'] = status
            event['actual_model'] = safe_model_id(body.get('model')) if isinstance(body, dict) else ''
            from .magpie import billing_route
            event['billing_route'] = billing_route(self.engine.config.settings, event['actual_model'], self.identities)
            self.store.count(self.job_id, 'mt_' + event['billing_route'] + '_response')
            event['entitlement_usage'] = event['cash_spend'] = 'unknown'
            raw_usage = body.get('usage') if isinstance(body, dict) else None
            if (isinstance(raw_usage, dict) and type(raw_usage.get('prompt_tokens')) is int and
                    type(raw_usage.get('completion_tokens')) is int and
                    0 <= raw_usage['prompt_tokens'] <= 2**31 - 1 and 0 <= raw_usage['completion_tokens'] <= 2**31 - 1):
                usage = {'input_tokens': raw_usage['prompt_tokens'], 'output_tokens': raw_usage['completion_tokens']}
                event.update(usage)
                for field, amount in usage.items():
                    self.store.count(self.job_id, 'mt_response_' + field, amount)
                details = raw_usage.get('completion_tokens_details')
                reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
                if type(reasoning) is int and 0 <= reasoning <= usage['output_tokens']:
                    event['reasoning_tokens'] = reasoning
                    self.store.count(self.job_id, 'mt_response_reasoning_tokens', reasoning)
            else:
                usage = None
                self.store.count(self.job_id, 'mt_response_usage_unknown')
            if 200 <= status < 300:
                self.store.count(self.job_id, 'mt_success')
            if status == 429:
                self.store.count(self.job_id, 'mt_429')
            category = service_failure(status, body)
            if category is not None:
                scope = 'entry'
                if status == 401 or category == 'authentication':
                    category, scope = 'authentication', 'credential'
                elif entry.tier == 'paid' and (status == 402 or category == 'payment_required'):
                    category, scope = 'payment_required', 'paid_account'
                elif status == 404:
                    scope = 'model'
                outcome, event['category'] = 'service_error', category
                raise RouteFailure(category, scope=scope) from None
            try:
                value = parse_stage_response(name, body, ids=ids, originals=originals)
                if name == 'judge':
                    validate_judgment(value, data)
            except BatchValidationError as error:
                outcome, event['validation_category'] = 'invalid', error.category
                self.store.count(self.job_id, 'mt_invalid_response')
                if error.category == 'output_truncated':
                    self.store.count(self.job_id, 'mt_output_truncated')
                metadata = batch_response_metadata(body)
                self.diagnostics.write({'kind': 'invalid_response', **event, 'category': error.category,
                    'expected_count': len(ids), 'actual_count': error.actual_count, **metadata})
                # Regenerate a normally completed response from original
                # evidence when local format validation fails.
                completed = metadata['finish_reason'] == 'stop' and metadata['content_type'] == 'string'
                repairable = name in {'translate'} and completed
                raise RouteFailure('output_truncated' if error.category == 'output_truncated' else
                                   'format_exhausted', repairable=repairable,
                                   fallback_allowed=completed or error.category == 'output_truncated') from None
            try:
                event['resolved_model'] = resolve_model(event['actual_model'], reply.headers, self.identities, require_admitted=True)
                if dynamic_model(event['actual_model']) and name != 'translate':
                    raise ValueError()
            except ValueError:
                outcome = 'invalid'
                raise RouteFailure('model_not_independent', fallback_allowed=True) from None
            if not _independent_model(event['resolved_model'], forbidden_models):
                outcome = 'invalid'
                raise RouteFailure('model_not_independent', fallback_allowed=True)
            if usage is None:
                self.store.count(self.job_id, 'mt_usage_unknown')
            else:
                for field, amount in usage.items():
                    self.store.count(self.job_id, 'mt_' + field, amount)
            if attempt == 2:
                self.store.count(self.job_id, 'mt_format_repair_success')
            if entry.id.endswith('_fallback'):
                self.store.count(self.job_id, 'mt_paid_fallback_success')
            outcome = 'valid'
            return {'value': value, 'actual_model': event['actual_model'],
                    'resolved_model': event['resolved_model'], 'usage': usage}
        except RouteFailure as error:
            error.request_id = request_id
            raise
        except Cancelled:
            outcome = 'cancelled'
            raise
        finally:
            self.diagnostics.write_route({'kind': 'request_end', **event, 'outcome': outcome,
                'duration_ms': max(0, round((time.monotonic() - started) * 1000))})

    def _fit(self, stage, data, *, output_budget=False, fixed_semantics=False):
        """Fit context and glossary to the budget while preserving targets."""
        data = dict(data)
        candidates = []
        for entry in self.router_for(stage).entries:
            try:
                entry.backend.credential(self.engine.config)
            except ToolError:
                continue
            candidates.append(entry)
        output_estimate = estimate_stage_output(stage, data) if output_budget else None
        def payload_fits(entry):
            capabilities = self.profiles[entry.id]['capabilities']
            if output_budget and output_estimate > min(4096, capabilities['output_tokens']) * 3 // 4:
                raise ToolError('翻译组预计输出超过规划余量。', 'mt_input_budget')
            # Reserve the fixed repair instruction without dropping evidence
            # between the initial request and its possible regeneration.
            make_stage_payload(stage, data, entry.backend, capabilities=capabilities,
                               repair=stage in {'translate'})
        while True:
            fits = bool(candidates)
            for entry in candidates:
                try:
                    payload_fits(entry)
                except ToolError as error:
                    if error.code != 'mt_input_budget':
                        raise
                    fits = False
                    break
            if fits:
                return data
            smaller_context = trim_context(data.get('context', [])) if not fixed_semantics else None
            if smaller_context is not None:
                data['context'] = smaller_context
            elif data.get('glossary') and not fixed_semantics:
                data['glossary'] = data['glossary'][:-1]
            else:
                raise ToolError('翻译阶段完整请求超过所有可用模型的输入预算。', 'mt_input_budget')

    def _work_key(self, name, data):
        return _digest({'strategy': 6, 'prompt': QUALITY_PROMPT_VERSION, 'stage': name, 'data': data,
                        'scope': self.cache_scope, 'profiles': self.snapshot['entries']})

    def stage(self, name, data, *, ids=(), originals=(), saved_only=False, fixed_semantics=False,
              forbidden_models=()):
        self.engine.mt_control.check_start()
        forbidden_models = frozenset(forbidden_models)
        if name == 'glossary':
            ids = [item['id'] for item in data['sentences']]
            originals = [item['text'] for item in data['sentences']]
        group = getattr(self.context, 'group', None)
        if self.group_progress is not None and group is not None:
            self.group_progress.set(group, name)
        started = time.monotonic()
        deadline = started + STAGE_TIMEOUT
        work_key = self._work_key(name, data)
        self.context.stage, self.context.group_key = name, work_key
        self.context.request_id = self.context.gateway_session = None
        # Bind numbered draft outputs to source text, including on cross-task
        # cache hits. Hashes allow partial layout replacement without retaining
        # another copy of the transcript or binding reusable stages to cue IDs.
        evidence = ({'source_text_hashes': [_digest(text.strip()) for text in originals]}
                    if name == 'translate' else {})
        input_fields = {'context'} if name == 'glossary' else set()
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
                    set(saved) - {'resolved_model'} != {'value', 'backend', 'request_model', 'actual_model', 'usage', 'stage'} | set(evidence) | input_fields or
                    any(saved[k] != value for k, value in evidence.items()) or
                    saved.get('stage') != name or not isinstance(saved.get('backend'), str) or
                    saved.get('backend') not in self.profiles or
                    saved.get('request_model') != self.profiles[saved['backend']]['backend']['model'] or
                    not isinstance(saved.get('actual_model'), str) or
                    safe_model_id(saved['actual_model']) != saved['actual_model'] or
                    not _valid_usage(usage)):
                raise ToolError('翻译阶段保存记录无效。', 'cache_mismatch')
            if name == 'glossary' and not valid_context_subset(data.get('context', []), saved['context']):
                raise ToolError('术语阶段保存的上下文无效。', 'cache_mismatch')
            entry = next((entry for entry in self.router_for(name).entries if entry.id == saved['backend']), None)
            if entry is not None:
                try:
                    entry.backend.credential(self.engine.config)
                except ToolError:
                    pass
                else:
                    try:
                        value = validate_stage_value(name, saved.get('value'), ids=ids, originals=originals)
                        if name == 'judge':
                            validate_judgment(value, data)
                    except ToolError:
                        raise ToolError('保存的翻译阶段记录无效。', 'cache_mismatch') from None
                    try:
                        resolved = _record_model(saved, self.identities)
                    except ValueError:
                        raise ToolError('保存的动态路由缺少可验证身份，请用 --reflow 从原始转写重建。',
                                        'cache_mismatch') from None
                    if not _independent_model(resolved, forbidden_models):
                        return None
                    return value, saved['backend'], {'key': work_key, 'actual_model': saved['actual_model'],
                                                     'resolved_model': resolved,
                                                     'request_model': saved['request_model']}
            return None

        saved = saved_stage()
        if saved is not None:
            return saved

        def cached(entry):
            try:
                entry.backend.credential(self.engine.config)
                key = stage_cache_key(name, data, entry.backend, capabilities=self.profiles[entry.id]['capabilities'],
                    scope=self.cache_scope)
            except ToolError as error:
                if error.code in {'mt_input_budget', 'credentials', 'mt_account_changed'}:
                    return None
                raise
            raw = self.store.cache_get(key)
            if raw is None:
                return None
            try:
                record = json.loads(raw)
                if (not isinstance(record, dict) or set(record) - {'resolved_model'} != {'value', 'actual_model', 'usage'} or
                        not isinstance(record['actual_model'], str) or
                        safe_model_id(record['actual_model']) != record['actual_model'] or
                        not _valid_usage(record['usage'])):
                    raise ValueError()
                value = validate_stage_value(name, record['value'], ids=ids, originals=originals)
                if name == 'judge':
                    validate_judgment(value, data)
                resolved = _record_model(record, self.identities)
                if not _independent_model(resolved, forbidden_models):
                    return None
                return {'value': value, 'actual_model': record['actual_model'], 'resolved_model': resolved, 'usage': None}
            except (ValueError, TypeError, KeyError, ToolError):
                raise ToolError('翻译阶段缓存无效，未复用。', 'cache_mismatch') from None

        def invoke(entry, timeout, attempt):
            return self._invoke_stage(name, data, ids, originals, work_key, entry, timeout, attempt,
                                      forbidden_models=forbidden_models)

        # Preserve the exact originally requested context and glossary for both
        # free and paid cache lookup before adapting to permitted network models.
        result = self.router_for(name).cached_result(cached, deadline=deadline)
        if result is None:
            try:
                data = self._fit(name, data, fixed_semantics=fixed_semantics)
            except ToolError as error:
                if saved_only and error.code == 'mt_input_budget':
                    return None
                raise
            new_key = self._work_key(name, data)
            adapted = new_key != work_key
            if adapted and name != 'glossary':
                work_key = new_key
                self.context.group_key = work_key
                saved = saved_stage()
                if saved is not None:
                    return saved
            if adapted:
                result = self.router_for(name).cached_result(cached, deadline=deadline)
            if result is None:
                if saved_only:
                    return None
                result = self.router_for(name).run(invoke, validate=self._validate_gateway, deadline=deadline)
        self.diagnostics.write_route({'kind': 'route_complete', 'stage': name, 'entry': result.entry.id,
            'model': result.entry.backend.model, 'actual_model': result.value['actual_model'],
            'resolved_model': result.value.get('resolved_model'),
            'gateway_session': self.context.gateway_session, 'request_id': self.context.request_id,
            'duration_ms': round((time.monotonic() - started) * 1000),
            'attempt': result.attempts, 'cached': result.cached,
            'group_key': work_key, 'run_id': getattr(self.engine, 'run_id', '')})
        record = {**result.value, **evidence, 'backend': result.entry.id, 'request_model': result.entry.backend.model,
                  'stage': name}
        if name == 'glossary':
            # Bind the accepted advisory result to its original source request,
            # keeping its actual trimmed context for inspection and validation.
            # Its cross-task cache still keys the exact fitted wire request.
            record['context'] = data.get('context', [])
        cache_key = stage_cache_key(name, data, result.entry.backend,
                                    capabilities=self.profiles[result.entry.id]['capabilities'], scope=self.cache_scope)
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_inflight()
            self.store.commit_mt_stage(self.job_id, work_key, name, record,
                cache_key=None if result.cached else cache_key,
                cache_value=None if result.cached else json.dumps(result.value, ensure_ascii=False),
                counts={'mt_v6_' + name + ('_cache_hit' if result.cached else '_completed'): 1})
        proof = {'key': work_key, 'actual_model': record['actual_model'], 'request_model': record['request_model'],
                 'resolved_model': _record_model(record, self.identities)}
        if not result.cached:
            proof.update(request_id=self.context.request_id, gateway_session=self.context.gateway_session)
        return record['value'], record['backend'], proof

    def _fits_group(self, units, *, output_budget=False):
        if units[-1]['cues'][-1]['end_ms'] - units[0]['cues'][0]['begin_ms'] > 60000:
            return False
        sentences = [_sentence(unit, str(i)) for i, unit in enumerate(units)]
        fragments = [{'id': str(n), 'sentence': str(i), 'text': cue['text']}
                     for n, (i, cue) in enumerate((i, cue) for i, unit in enumerate(units) for cue in unit['cues'])]
        context = sentence_context(units)
        samples = [('glossary', {'sentences': sentences, 'manual_glossary': [], 'context': context})]
        samples.extend(_group_samples(sentences, fragments, context, []))
        try:
            for stage, data in samples:
                self._fit(stage, data, output_budget=output_budget)
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
                def split(start, end):
                    unit = make_unit(source, members, start, end)
                    if self._fits_group([unit], output_budget=True):
                        return [unit]
                    if end - start < 2 or not source.get('words'):
                        # Intact targets use input-only admission, including guarded models.
                        if self._fits_group([unit]):
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
                if pending and not self._fits_group([*pending, unit], output_budget=True):
                    result.append(pending)
                    pending = []
                pending.append(unit)
            if pending:
                result.append(pending)
        return result

    def groups_for_layout(self, sources, cues, manifest):
        if 'mt_group_plan' in manifest:
            return restore_group_plan(manifest['mt_group_plan'], sources, cues, manifest['layout_round'])
        groups = self.plan_groups(sources, cues)
        candidate = copy.deepcopy(manifest)
        candidate['mt_group_plan'] = save_group_plan(groups, sources, cues, manifest['layout_round'])
        self.engine.commit_local(self.job_id, manifest, candidate, self.store.save_mt_group_plan)
        return groups

    def glossary_data(self, group, context, known=()):
        manual = self.snapshot['quality']['manual_glossary']
        by_source = {}
        for unit in group:
            identity = unit['source']['sentence_id']
            if identity not in by_source:
                by_source[identity] = _sentence(unit, identity)
            else:
                # Distinct adjacent source ranges can contain identical words.
                # Preserve both occurrences and their exact source spacing.
                current = by_source[identity]
                following = _sentence(unit, identity)
                start = min(current.get('char_start', 0), following.get('char_start', 0))
                end = max(current.get('char_end', len(current['text'])),
                          following.get('char_end', len(following['text'])))
                current.update(text=unit['source']['text'][start:end], char_start=start, char_end=end)
        local = [{**unit['source'], 'text': unit['text']} for unit in group]
        return {'sentences': list(by_source.values()), 'manual_glossary': manual_terms(local, manual),
                'context': context, 'glossary': list(known)}

    def glossary(self, group, context, known=()):
        data = self.glossary_data(group, context, known)
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
            targets = {item['id']: item['text'] for item in data['sentences']}
            return [{**term, 'origin': 'model', 'evidence': term_evidence(term['source'], targets[term['sentence']])}
                    for term in result['terms']]
        except RouteExhausted:
            self.engine.mt_control.check_start()
            # Glossary extraction is advisory; translations still use evidenced manual entries.
            with self.engine.mt_control.lock:
                self.engine.mt_control.check_start()
                self.store.commit_mt_stage(self.job_id, unavailable_key, 'glossary_unavailable',
                                           {'stage': 'glossary_unavailable'},
                                           counts={'mt_glossary_unavailable': 1})
            return []

    def _semantic_input(self, sentences, fragments, context, glossary):
        """Freeze evidence before the draft; resume must not reselect it by live capacity."""
        original = {'sentences': sentences, 'fragments': fragments, 'context': context, 'glossary': glossary}
        key = self._work_key('semantic_input', original)
        try:
            saved = self.store.mt_stage_entry(self.job_id, key)
            if saved is not None:
                record = saved['record']
                if (saved['stage'] != 'semantic_input' or not isinstance(record, dict) or
                        set(record) != {'stage', 'context', 'glossary'} or record['stage'] != 'semantic_input'):
                    raise ValueError()
                if any(not valid_context_subset(original[field], record[field]) for field in ('context', 'glossary')):
                    raise ValueError()
                return record['context'], record['glossary']
        except (KeyError, TypeError, ValueError, RecursionError):
            raise ToolError('保存的翻译语义输入与来源不一致。', 'cache_mismatch') from None
        for name, sample in _group_samples(sentences, fragments, context, glossary):
            fitted = self._fit(name, {**sample, 'context': context, 'glossary': glossary})
            context, glossary = fitted['context'], fitted['glossary']
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_start()
            self.store.commit_mt_stage(self.job_id, key, 'semantic_input',
                                       {'stage': 'semantic_input', 'context': context, 'glossary': glossary})
        return context, glossary

    def group(self, group, glossary, context):
        self.engine.mt_control.check_start()
        members = [cue for unit in group for cue in unit['cues']]
        if not members or all(cue['translated'] for cue in members):
            return
        relevant = [term for term in glossary if any(term_appears(term['source'], unit['text']) for unit in group)]
        local_ids = {unit['source']['sentence_id'] for unit in group}
        relevant.sort(key=lambda term: (term['origin'] != 'manual', term['sentence'] not in local_ids))
        sentences = [_sentence(unit, str(i)) for i, unit in enumerate(group)]
        source_index = {cue['cue_id']: str(i) for i, unit in enumerate(group) for cue in unit['cues']}
        fragments = [{'id': str(i), 'sentence': source_index[cue['cue_id']], 'text': cue['text']}
                     for i, cue in enumerate(members)]
        # Choose one shared subset that fits every stage skeleton. Actual output
        # may still exceed the budget; later stages must not silently discard
        # evidence on which the accepted draft was based.
        context, relevant = self._semantic_input(sentences, fragments, context, relevant)
        ids, originals = [f['id'] for f in fragments], [f['text'] for f in fragments]
        data = {'sentences': sentences, 'fragments': fragments, 'glossary': relevant, 'context': context}
        translated, origin, translation_proof = self.stage('translate', data, ids=ids, originals=originals,
                                                          fixed_semantics=True)
        production = translation_proof
        current = translated['translations']
        warning = False
        stalled = False
        previous_correction = None
        review_keys, correction_keys, reviewed = [], [], []
        for round_number in range(self.snapshot['quality']['max_revisions'] + 1):
            self.engine.mt_control.check_start()
            # Repeated subtitles have the same review request identity. A
            # no-op correction or a cycle would only replay saved issues.
            if current in reviewed:
                warning = True
                break
            review_data = {**data, 'translations': current}
            forbidden_models = {_proof_model(translation_proof)}
            if production is not translation_proof:
                forbidden_models.add(_proof_model(production))
            try:
                review, _, review_proof = self.stage('review', review_data, ids=ids,
                                                    fixed_semantics=True, forbidden_models=forbidden_models)
                review_keys.append(review_proof['key'])
                reviewed.append(current)
            except RouteExhausted:
                self.engine.mt_control.check_start()
                warning = True
                break
            if not review['issues']:
                break
            if round_number == self.snapshot['quality']['max_revisions']:
                warning = True
                break
            try:
                correction_data = {**review_data, 'issues': review['issues']}
                forbidden_models = {_proof_model(translation_proof), _proof_model(review_proof)}
                stalled = correction_stalled(previous_correction, correction_data)
                correction = self.stage('judge', correction_data, ids=ids, originals=originals,
                                        saved_only=stalled, fixed_semantics=True,
                                        forbidden_models=forbidden_models)
                if correction is None:
                    warning = True
                    break
                stalled = False  # A complete saved correction still takes precedence.
                corrected, origin, production = correction
                correction_keys.append(production['key'])
                previous_correction = correction_data
                current = corrected['translations']
                if not corrected['issues']:
                    break
            except RouteExhausted:
                self.engine.mt_control.check_start()
                warning = True
                break
        final_key = _digest({'stage': 'final', 'strategy': 6, 'cues': [c['cue_id'] for c in members],
                             'data': data, 'scope': self.cache_scope, 'profiles': self.snapshot['entries']})
        record = {'backend': origin, 'request_model': production['request_model'],
                  'actual_model': production['actual_model'], 'production_stage_key': production['key'],
                  'resolved_model': _proof_model(production),
                  'translation_stage_key': translation_proof['key'], 'review_stage_keys': review_keys,
                  'correction_stage_keys': correction_keys, 'cue_ids': [cue['cue_id'] for cue in members],
                  'value': {'translations': current}, 'warning': warning, 'stage': 'final'}
        values = {cue['cue_id']: current[str(i)] for i, cue in enumerate(members)}
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_inflight()
            counts = {'mt_subtitle_completed': len(values), 'mt_quality_warning': int(warning)}
            if stalled:
                counts['mt_correction_stalled'] = 1
            self.store.commit_mt_stage(self.job_id, final_key, 'final', record, translations=values, origin=origin,
                                       counts=counts)


def process_subtitles(engine, job_id, manifest, settings, scope, stage):
    sources = engine.store.sources(job_id)
    if not sources:
        raise ToolError('任务缺少完整句来源记录。', 'cache_mismatch')
    if manifest.get('layout_complete'):
        if any(cue['decision'] == 'translate' and not cue['translated'] for cue in engine.store.cues(job_id)):
            raise ToolError('已完成排版缺少完整译文。', 'cache_mismatch')
        return
    source_ids = {source['sentence_id']: source for source in sources}
    positions = {source['sentence_id']: index for index, source in enumerate(sources)}
    pipeline = Pipeline(engine, job_id, manifest, scope)
    try:
        if not manifest.get('layout_planned'):
            cues = []
            for source in sources:
                parts = plan_fragments(source, settings)
                if source['decision'] == 'translate':
                    parts = split_mt_units(source, parts, fits=lambda cue: pipeline._fits_group(
                        [{'source': source, 'text': cue['text'], 'cues': [cue]}]))
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
            groups = pipeline.groups_for_layout(sources, cues, manifest)
            pending = [index for index, group in enumerate(groups)
                       if any(not cue['translated'] for unit in group for cue in unit['cues'])]
            if pending:
                # Completed earlier groups still contribute their exact saved glossary
                # to the prefix used by later unfinished groups.
                # Retain completed tail groups too: truncating a window changes
                # its request identity and the frozen terminology on resume.
                progress = GroupProgress(engine, job_id)
                pipeline.group_progress = progress
                engine.progress(job_id, 'subtitle_translating',
                                sum(c['decision'] != 'translate' or bool(c['translated']) for c in cues), len(cues))
                progress.start()
                try:
                    _translate_groups(pipeline, groups, sources, positions,
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
    finally:
        pipeline.close()


def _translate_groups(pipeline, groups, sources, positions, *, done, total):
    """Schedule ready translations first; prefix waits never occupy a worker."""
    engine, job_id = pipeline.engine, pipeline.job_id
    progress = pipeline.group_progress
    workers = min(engine.config.settings.mt_workers, max(1, len(groups)))
    prefix = GlossaryPrefix(pipeline, sources)
    glossaries, errors = {}, []
    next_glossary = next_group = 0
    extracting_window = False

    def window_end(start, known):
        end = min(start + 8, len(groups))
        while end > start + 1:
            units = [unit for group in groups[start:end] for unit in group]
            try:
                pipeline._fit('glossary', pipeline.glossary_data(
                    units, group_context(sources, units, positions), known), output_budget=True)
                break
            except ToolError as error:
                if error.code != 'mt_input_budget':
                    raise
                end -= 1
        return end

    def extract(start, end, known):
        engine.mt_control.check_start()
        pipeline.context.group = start
        progress.set(start, 'glossary')
        units = [unit for group in groups[start:end] for unit in group]
        result = pipeline.glossary(units, group_context(sources, units, positions), known)
        assigned = {i: [] for i in range(start, end)}
        for term in result:
            for index in range(start, end):
                if any(unit['source']['sentence_id'] == term['sentence'] and
                       term_appears(term['source'], unit['text']) for unit in groups[index]):
                    assigned[index].append(term)
                    break
        progress.set(start, 'waiting')
        return assigned

    def translate(index, glossary):
        engine.mt_control.check_start()
        pipeline.context.group = index
        group = groups[index]
        context = group_context(sources, group, positions)
        pipeline.group(group, glossary, context)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}

        def schedule():
            nonlocal next_glossary, next_group, extracting_window
            while len(futures) < workers and not errors and not engine.mt_control.stopping:
                if next_group in glossaries:
                    index = next_group
                    # Freeze only the prefix about to be consumed, keeping both
                    # terminology and retained memory independent of completion order.
                    glossary = prefix.extend(glossaries.pop(index))
                    next_group += 1
                    members = [cue for unit in groups[index] for cue in unit['cues']]
                    if all(cue['translated'] for cue in members):
                        progress.finish(index)
                        continue
                    futures[executor.submit(translate, index, glossary)] = (index, False)
                elif (not extracting_window and next_glossary < len(groups) and
                      next_glossary == next_group and
                      sum(not extracting for _, extracting in futures.values()) < 16):
                    # A single ordered extractor sees the complete prior prefix.
                    # At most two eight-group windows overlap with translations.
                    index = next_glossary
                    known = tuple(prefix.terms)
                    end = window_end(index, known)
                    next_glossary = end
                    extracting_window = True
                    futures[executor.submit(extract, index, end, known)] = (index, True)
                else:
                    break

        try:
            schedule()
            while futures:
                engine.mt_control.stopping  # Observe external cancellation during silent requests.
                completed, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, extracting = futures.pop(future)
                    try:
                        value = future.result()
                        if extracting:
                            glossaries.update(value)
                            extracting_window = False
                        else:
                            done += sum(not cue['translated'] for unit in groups[index] for cue in unit['cues'])
                    except ToolError as error:
                        progress.finish(index)
                        if error.code != 'cancelled':
                            engine.mt_control.stop(error)
                        errors.append(error)
                    if not extracting:
                        progress.finish(index)
                        engine.progress(job_id, 'subtitle_translating', done, total)
                schedule()
        except ToolError as error:
            # Cancellation can race with prefix consumption on this thread.
            # Keep the normal drain window for requests already dispatched.
            if error.code != 'cancelled':
                engine.mt_control.stop(error)
            errors.append(error)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, Cancelled)):
                engine.request_cancel(force=True)
            else:
                engine.mt_control.stop(ToolError('翻译阶段本地处理失败，进度已保存。', 'local_io'), force=True)
            raise
    check_cancel(engine.cancel)
    if errors:
        raise errors[0]
