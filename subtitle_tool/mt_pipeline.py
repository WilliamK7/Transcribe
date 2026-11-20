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
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from .common import MAX_LAYOUT_ROUNDS, PROCESSING_VERSION, Cancelled, ToolError, check_cancel
from .mt_backend import MTBackend
from .mt_context import group_context, sentence_context, target_span, trim_context, valid_context_subset
from .mt_diagnostics import MTDiagnostics, batch_response_metadata, safe_model_id
from .mt_pool import saved_route
from .mt_planning import make_translation_unit, restore_group_plan, save_group_plan
from .mt_quality import (BatchValidationError, QUALITY_PROMPT_VERSION,
                         estimate_stage_output, make_stage_payload, manual_terms, parse_stage_response, plan_sentence_groups,
                         repetition_checks, review_with_checks, request_cache_key, term_appears, term_evidence,
                         validate_stage_value, validate_judgment, validate_translation_content)
from .mt_routing import STAGE_TIMEOUT, MagpieRoute, RouteEntry, RouteExhausted, RouteFailure
from .mt_runtime import service_failure
from .mt_transport import TransportError
from .mt_identity import canonical_model, resolve_model, dynamic_model
from .mt_fidelity import FIDELITY_VERSION, source_anchors, anchor_checks
from .review import decision_origin
from .subtitles import (layout_warnings, plan_display_timing, plan_fragments, revise_layout, split_cues_for_translation,
                        timestamp, validate_cue_coverage)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _independent_model(model, forbidden):
    return bool(model and not model.startswith('group/') and not dynamic_model(model) and
                canonical_model(model) not in forbidden)


def _record_model(record, identities):
    resolved = record['resolved_model']
    if not isinstance(resolved, str):
        raise ValueError('Missing saved model identity')
    return resolve_model(record['actual_model'], {}, identities, saved=resolved, require_admitted=True)


def _proof_model(proof):
    resolved = proof['resolved_model']
    if not isinstance(resolved, str):
        raise ValueError('Missing production model identity')
    return resolved


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


def _valid_stage_metadata(record):
    model, usage = record['actual_model'], record['usage']
    return (isinstance(model, str) and safe_model_id(model) == model and
            (usage is None or isinstance(usage, dict) and set(usage) == {'input_tokens', 'output_tokens'} and
             all(type(count) is int and count >= 0 for count in usage.values())))


@dataclass(frozen=True)
class StageRequest:
    name: str
    data: dict
    ids: tuple
    originals: tuple
    forbidden_models: frozenset
    check_content: bool = False
    fidelity: bool = False

    def validate_content(self, value):
        if self.check_content and self.name in {'translate', 'judge'}:
            validate_translation_content(value['translations'], self.originals)
        if self.name == 'judge':
            validate_judgment(value, self.data, fidelity=self.fidelity)

    def validate_record(self, record, identities):
        value = validate_stage_value(self.name, record['value'], ids=self.ids, originals=self.originals,
                                     fidelity=self.fidelity)
        self.validate_content(value)
        resolved = _record_model(record, identities)
        if not _independent_model(resolved, self.forbidden_models):
            return None
        return value, resolved


def _sentence(translation_unit, identity):
    source = translation_unit['source']
    start, end = target_span(translation_unit)
    return {'id': identity, 'text': translation_unit['text'], 'asr_language': source.get('language', ''),
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
    from .policy import subtitle_contract
    scope = {**identity, 'processing_version': PROCESSING_VERSION}
    manifest = {**scope, **subtitle_contract(config.settings), 'mt_route_snapshot': route}
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


def _quality_checks(translations, source_hashes, anchors=None):
    checks = repetition_checks(translations, source_hashes)
    return checks + (anchor_checks(translations, anchors) if anchors is not None else [])


def _local_review_record(translations, source_hashes, anchors=None):
    return {'stage': 'local_review', 'value': {'issues': _quality_checks(translations, source_hashes, anchors)},
            'source_text_hashes': source_hashes, 'translations_digest': _digest(translations),
            **({'source_anchors': anchors} if anchors is not None else {})}


def _only_source_uncertain(issues):
    return bool(issues) and all(issue['category'] == 'source_uncertain' for issue in issues)


def _remember_uncertainty(issues, translations, fragments, confirmed):
    sentences = {fragment['id']: fragment['sentence'] for fragment in fragments}
    for issue in issues:
        if issue['category'] == 'source_uncertain':
            confirmed[issue['id']] = {key: translations[key] for key, sentence in sentences.items()
                                      if sentence == sentences[issue['id']]}


def _confirmed_uncertainty(issues, translations, confirmed):
    return _only_source_uncertain(issues) and all(issue['id'] in confirmed and all(
        translations[key] == value for key, value in confirmed[issue['id']].items()) for issue in issues)


def _validate_saved_reviews(final, history, source_hashes, *, fidelity=False, allow_source_uncertain=False,
                            allow_quality_warning=False):
    reviews, corrections = final['review_stage_keys'], final['correction_stage_keys']
    if not len(corrections) <= len(reviews) <= len(corrections) + 1:
        raise ValueError()
    # A later layout may have replaced some cues; their stable IDs still carry the original sentence.
    fragments = [{'id': str(index), 'sentence': cue_id.rpartition(':')[0]}
                 for index, cue_id in enumerate(final['cue_ids'])]
    ids = {fragment['id'] for fragment in fragments}
    current = history[final['translation_stage_key']]['value']['translations']
    if not isinstance(current, dict) or set(current) != ids:
        raise ValueError()
    unresolved, accepted_uncertainty = True, False
    confirmed_uncertainty = {}
    anchors = history[final['translation_stage_key']].get('source_anchors') if fidelity else None
    if fidelity and (not isinstance(anchors, list) or len(anchors) != len(ids) or any(
            not isinstance(group, list) or any(not isinstance(a, str) or not re.fullmatch(r'[a-z]{1,2}\d{1,3}', a)
                                               for a in group) for group in anchors)):
        raise ValueError()
    for index, review_key in enumerate(reviews):
        record = history[review_key]
        local = record['stage'] == 'local_review'
        if local and (record != _local_review_record(current, source_hashes, anchors) or not record['value']['issues']):
            raise ValueError()
        review = validate_stage_value('review', record['value'], ids=ids, fidelity=fidelity and not local)
        review = review_with_checks(review, _quality_checks(current, source_hashes, anchors))
        unresolved = bool(review['issues'])
        accepted_uncertainty = (allow_source_uncertain and not local and
                               _confirmed_uncertainty(review['issues'], current, confirmed_uncertainty))
        if index == len(corrections):
            break
        if not unresolved:
            raise ValueError()
        correction = history[corrections[index]]['value']
        validate_stage_value('review', {'issues': correction['issues']}, ids=ids,
                             fidelity=fidelity, review_coverage=False)
        if not isinstance(correction['translations'], dict) or set(correction['translations']) != ids:
            raise ValueError()
        validate_judgment(correction, {**review, 'fragments': fragments, 'translations': current}, fidelity=fidelity)
        current = correction['translations']
        if not local:
            _remember_uncertainty(correction['issues'], current, fragments, confirmed_uncertainty)
        # A local check covers its own signal only; a dismissed signal cannot
        # certify the rest of the group as independently reviewed.
        unresolved = bool(correction['issues']) or local
        accepted_uncertainty = allow_source_uncertain and not local and _only_source_uncertain(correction['issues'])
        if (not correction['issues'] or accepted_uncertainty) and index + 1 < len(reviews):
            raise ValueError()
    if final['warning'] != unresolved or fidelity and unresolved and not (accepted_uncertainty or allow_quality_warning):
        raise ValueError()


def validate_provenance(engine, job_id, manifest, *, verify_credentials=True):
    """Verify each saved cue is linked to its actual production stage and credential."""
    route = saved_route(manifest)
    profiles = {entry['id']: entry for entry in route['entries']}
    checked, finals = set(), {}
    fidelity = manifest.get('execution_snapshot', {}).get('fidelity_version') == FIDELITY_VERSION
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
                warning_fields = {'warning_reason', 'warning_ids'} if 'warning_reason' in final else set()
                if (set(final) != required | {'resolved_model'} | warning_fields or final['stage'] != 'final' or type(final['warning']) is not bool or
                        not isinstance(final['actual_model'], str) or
                        safe_model_id(final['actual_model']) != final['actual_model'] or
                        not isinstance(final['cue_ids'], list) or not final['cue_ids'] or
                        any(not isinstance(identity, str) for identity in final['cue_ids']) or
                        len(set(final['cue_ids'])) != len(final['cue_ids']) or
                        not isinstance(final['value'], dict) or set(final['value']) != {'translations'} or
                        not isinstance(final['value']['translations'], dict) or
                        set(final['value']['translations']) != {str(i) for i in range(len(final['cue_ids']))}):
                    raise ValueError()
                if warning_fields and (route['quality'].get('on_failure') != 'warn' or not final['warning'] or
                        final['warning_reason'] not in {'review_unavailable', 'judge_unavailable', 'review_incomplete',
                                                       'source_uncertain', 'revision_cycle', 'revision_limit'} or
                        not isinstance(final['warning_ids'], list) or
                        any(identity not in final['value']['translations'] for identity in final['warning_ids'])):
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
                if _record_model(proof['record'], identities) != _record_model(final, identities):
                    raise ValueError()
                resolved_roles = {}
                history = {}
                for stage_name, keys in [('translate', [final['translation_stage_key']]),
                                         ('review', final['review_stage_keys']),
                                         ('judge', final['correction_stage_keys'])]:
                    if not isinstance(keys, list):
                        raise ValueError()
                    for stage_key in keys:
                        entry = engine.store.mt_stage_entry(job_id, stage_key)
                        if (stage_name == 'review' and isinstance(entry, dict) and entry.get('stage') == 'local_review' and
                                manifest['execution_snapshot'].get('local_review_version') == 1):
                            if not isinstance(entry.get('record'), dict) or entry['record'].get('stage') != 'local_review':
                                raise ValueError()
                            history[stage_key] = entry['record']
                            continue
                        if (not isinstance(entry, dict) or entry['stage'] != stage_name or
                                not isinstance(entry['record'], dict) or entry['record'].get('stage') != stage_name):
                            raise ValueError()
                        resolved = _record_model(entry['record'], route.get('identities', {}))
                        if dynamic_model(entry['record']['actual_model']) and stage_name != 'translate':
                            raise ValueError()
                        if resolved in resolved_roles and resolved_roles[resolved] != stage_name:
                            raise ValueError()
                        resolved_roles[resolved] = stage_name
                        history[stage_key] = entry['record']
                corrections = final['correction_stage_keys']
                if final['production_stage_key'] != (corrections[-1] if corrections else final['translation_stage_key']):
                    raise ValueError()
                source_hashes = history[final['translation_stage_key']].get('source_text_hashes')
                if (not isinstance(source_hashes, list) or len(source_hashes) != len(final['cue_ids']) or
                        any(not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) for digest in source_hashes)):
                    raise ValueError()
                _validate_saved_reviews(final, history, source_hashes, fidelity=fidelity,
                                        allow_source_uncertain=route['quality'].get('source_uncertain') == 'warn',
                                        allow_quality_warning=route['quality'].get('on_failure') == 'warn')
                anchors = history[final['translation_stage_key']].get('source_anchors')
                finals[key] = final, source_hashes, anchors
            final, source_hashes, anchors = finals[key]
            if (final['backend'] != origin or final['request_model'] != profiles[origin]['backend']['model'] or
                    cue['cue_id'] not in final['cue_ids'] or
                    final['value']['translations'][str(final['cue_ids'].index(cue['cue_id']))] != cue['translated']):
                raise ValueError()
            if source_hashes[final['cue_ids'].index(cue['cue_id'])] != _digest(cue['text'].strip()):
                raise ValueError()
            if fidelity and anchors[final['cue_ids'].index(cue['cue_id'])] != source_anchors([cue['text']])[0]:
                raise ValueError()
            if manifest.get('execution_snapshot', {}).get('content_validation_version') == 2:
                validate_translation_content({'0': cue['translated']}, [cue['text']])
    except (KeyError, TypeError, ValueError, BatchValidationError):
        raise ToolError('译文阶段来源或底层型号与快照不一致；请用 --reflow 从原始转写重建。',
                        'cache_mismatch') from None


class Pipeline:
    def __init__(self, engine, job_id, manifest, scope):
        from .policy import load_policy
        self.engine, self.store, self.job_id = engine, engine.store, job_id
        load_policy(manifest)
        self.stream = manifest['execution_snapshot'].get('transport_version', 1) == 2
        self.check_content = manifest['execution_snapshot'].get('content_validation_version', 1) == 2
        self.local_review = manifest['execution_snapshot'].get('local_review_version') == 1
        self.fidelity = manifest['execution_snapshot'].get('fidelity_version') == FIDELITY_VERSION
        self.context = threading.local()
        self.group_progress = None
        self.display_durations = {}
        self.diagnostics = MTDiagnostics(engine.config.root, job_id)
        from . import mt_transport
        self.transport = mt_transport.ProcessTransport()
        self.maintenance_lock = threading.Lock()
        self.maintenance = None
        self.snapshot = saved_route(manifest)
        self.output_tokens = self.snapshot['quality']['output_tokens']
        self.allow_source_uncertain = self.fidelity and self.snapshot['quality'].get('source_uncertain') == 'warn'
        self.allow_quality_warning = self.fidelity and self.snapshot['quality'].get('on_failure') == 'warn'
        self.identities = self.snapshot.get('identities', {})
        self.cache_scope = dict(scope)
        if self.output_tokens != 4096:
            self.cache_scope['output_tokens'] = self.output_tokens
        if self.allow_source_uncertain:
            self.cache_scope['source_uncertain'] = 'warn'
        if self.check_content:
            self.cache_scope['content_validation'] = 2
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

    def router_for(self, stage):
        return self.routers['review' if stage in {'glossary', 'language'} else stage]

    def close(self):
        if self.maintenance is not None:
            trial, channels = self.maintenance
            trial.stop.set()
            channels.close()
            trial.close()

    def _prepare_maintenance(self, base_url):
        from .bailian_free import BailianFreeGuard
        from .mt_maintenance import ChannelMaintenance
        with self.maintenance_lock:
            if self.maintenance is None:
                trial = BailianFreeGuard(self.engine.config, self.engine.mt_control, job_id=self.job_id)
                channels = ChannelMaintenance(self.engine.config, self.engine.mt_control, trial_state=trial.trial_state)
                self.maintenance = trial, channels
            trial, channels = self.maintenance
        channels.prepare(base_url)
        try:
            trial.prepare(base_url)
            if trial.thread is not None:
                trial.poll_routes()
        except Cancelled:
            raise
        except ToolError:
            raise RouteFailure('permission_denied', fallback_allowed=True) from None

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
        from .magpie import bound_gateway, managed_gateway, validate_group, validate_role_pools
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
        # Live pool validation sees newly admitted members while saved-stage
        # evidence remains tied to the route snapshot.
        if not bound_gateway(self.engine.config.settings, entry.backend.base_url):
            return
        pool_identities = {**saved_catalog(self.engine.config).get('identities', {}), **self.identities}
        if not entry.id.endswith('_fallback') and managed_gateway(self.engine.config, entry.backend.base_url):
            self._prepare_maintenance(entry.backend.base_url)
        validate_role_pools(self.engine.config.settings, {
            role: [candidate.backend.model for candidate in router.entries
                   if candidate.backend.base_url == entry.backend.base_url] for role, router in self.routers.items()},
            base_url=entry.backend.base_url, identities=pool_identities)
        validate_group(self.engine.config.settings, entry.backend.model, base_url=entry.backend.base_url,
                       paid_only=entry.id.endswith('_fallback'), identities=self.identities)

    def _request_payload(self, stage, data, entry, *, repair=False):
        return make_stage_payload(stage, data, entry.backend,
                                  capabilities=self.profiles[entry.id]['capabilities'], repair=repair, stream=self.stream,
                                  fidelity=self.fidelity, output_tokens=self.output_tokens)

    def _invoke_stage(self, request, stage_key, entry, timeout, attempt):
        """One HTTP request, with content-free evidence even when validation fails."""
        name, data = request.name, request.data
        self.engine.mt_control.check_start()
        started = time.monotonic()
        try:
            key = entry.backend.credential(self.engine.config)
        except ToolError:
            raise RouteFailure('authentication', diagnostic_scope='credential') from None
        payload = self._request_payload(name, data, entry, repair=attempt == 2)
        request_id = uuid.uuid4().hex
        session = 'transcribe-' + self.job_id + '-' + request_id
        self.context.request_id, self.context.gateway_session = request_id, session
        event = {'stage': name, 'entry': entry.id, 'model': entry.backend.model,
                 'group_key': stage_key, 'run_id': getattr(self.engine, 'run_id', ''),
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
                raise RouteFailure(error.category, diagnostic_scope='endpoint' if error.category == 'network' else 'entry',
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
                if category == 'output_truncated':
                    self.store.count(self.job_id, 'mt_output_truncated')
                raise RouteFailure(category, diagnostic_scope=scope,
                                   fallback_allowed=category in {'output_truncated', 'generation_error'}) from None
            try:
                value = parse_stage_response(name, body, ids=request.ids, originals=request.originals, fidelity=request.fidelity,
                                             repair_data=data if name == 'judge' and
                                             (attempt == 2 or data.get('repair_reason') == 'no_progress') else None)
                request.validate_content(value)
            except BatchValidationError as error:
                outcome, event['validation_category'] = 'invalid', error.category
                self.store.count(self.job_id, 'mt_invalid_response')
                if error.category == 'output_truncated':
                    self.store.count(self.job_id, 'mt_output_truncated')
                metadata = batch_response_metadata(body)
                self.diagnostics.write({'kind': 'invalid_response', **event, 'category': error.category,
                    'expected_count': len(request.ids), 'actual_count': error.actual_count, **metadata})
                # Regenerate a normally completed response from original
                # evidence when local format validation fails.
                completed = metadata['finish_reason'] == 'stop' and metadata['content_type'] == 'string'
                repairable = completed and (name == 'translate' or name == 'judge' and self.fidelity)
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
            if not _independent_model(event['resolved_model'], request.forbidden_models):
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
            # Translation repair has always been part of group planning. Optional
            # judgment repair must not regroup saved work or trim frozen evidence.
            make_stage_payload(stage, data, entry.backend, capabilities=capabilities,
                               repair=stage == 'translate', stream=self.stream, fidelity=self.fidelity,
                               output_tokens=self.output_tokens)
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

    def _task_stage_key(self, name, data):
        return _digest({'strategy': 6, 'prompt': QUALITY_PROMPT_VERSION, 'stage': name, 'data': data,
                        'scope': self.cache_scope, 'profiles': self.snapshot['entries'],
                        **({'transport': 2} if self.stream else {}),
                        **({'fidelity': FIDELITY_VERSION} if self.fidelity else {})})

    def _read_task_stage(self, request, stage_key, evidence):
        try:
            stored = self.store.mt_stage_entry(self.job_id, stage_key)
            if stored is None:
                return None
            record = stored['record']
            fields = {'value', 'backend', 'request_model', 'actual_model', 'resolved_model', 'usage', 'stage'} | evidence.keys()
            if request.name == 'glossary':
                fields.add('context')
                if not valid_context_subset(request.data.get('context', []), record['context']):
                    raise ValueError()
            if (stored['stage'] != request.name or set(record) != fields or record['stage'] != request.name or
                    any(record[k] != v for k, v in evidence.items()) or
                    record['request_model'] != self.profiles[record['backend']]['backend']['model'] or
                    not _valid_stage_metadata(record)):
                raise ValueError()
            entry = next((e for e in self.router_for(request.name).entries if e.id == record['backend']), None)
            if entry is None:
                return None
            try:
                entry.backend.credential(self.engine.config)
            except ToolError:
                return None
            accepted = request.validate_record(record, self.identities)
            if accepted is None:
                return None
            value, resolved = accepted
            return value, entry.id, {'key': stage_key, 'actual_model': record['actual_model'],
                                     'resolved_model': resolved, 'request_model': record['request_model']}
        except (ValueError, TypeError, KeyError, ToolError):
            raise ToolError('保存的翻译阶段与请求不符，请用 --reflow 从原始转写重建。', 'cache_mismatch') from None

    def _read_request_cache(self, request, entry):
        try:
            entry.backend.credential(self.engine.config)
            key = request_cache_key(request.name, request.data, entry.backend, capabilities=self.profiles[entry.id]['capabilities'],
                                    scope=self.cache_scope, stream=self.stream, fidelity=self.fidelity,
                                    output_tokens=self.output_tokens)
        except ToolError as error:
            if error.code in {'mt_input_budget', 'credentials', 'mt_account_changed'}:
                return None
            raise
        raw = self.store.cache_get(key)
        if raw is None:
            return None
        try:
            record = json.loads(raw)
            if (not isinstance(record, dict) or set(record) != {'value', 'actual_model', 'resolved_model', 'usage'} or
                    not _valid_stage_metadata(record)):
                raise ValueError()
            accepted = request.validate_record(record, self.identities)
            if accepted is None:
                return None
            value, resolved = accepted
            return {'value': value, 'actual_model': record['actual_model'], 'resolved_model': resolved, 'usage': None}
        except (ValueError, TypeError, KeyError, ToolError):
            raise ToolError('翻译请求缓存与请求不符，请用 --reflow 检查来源。', 'cache_mismatch') from None

    def stage(self, name, data, *, ids=(), originals=(), fixed_semantics=False,
              forbidden_models=()):
        self.engine.mt_control.check_start()
        forbidden_models = frozenset(forbidden_models)
        if name in {'glossary', 'language'}:
            ids = [item['id'] for item in data['sentences']]
            originals = [item['text'] for item in data['sentences']]
        group = getattr(self.context, 'group', None)
        if self.group_progress is not None and group is not None:
            self.group_progress.set(group, name)
        started = time.monotonic()
        deadline = started + STAGE_TIMEOUT
        stage_key = self._task_stage_key(name, data)
        self.context.stage, self.context.group_key = name, stage_key
        self.context.request_id = self.context.gateway_session = None
        # Bind numbered draft outputs to source text, including on cross-task
        # cache hits. Hashes allow partial layout replacement without retaining
        # another copy of the transcript or binding reusable stages to cue IDs.
        evidence = ({'source_text_hashes': [_digest(text.strip()) for text in originals]}
                    if name in {'translate', 'language'} else {})
        if name == 'language':
            evidence['source_records'] = [_digest(item) for item in data['sentences']]
        if name == 'translate' and self.fidelity:
            evidence['source_anchors'] = source_anchors(originals)
        request = StageRequest(name, data, tuple(ids), tuple(originals), forbidden_models, self.check_content, self.fidelity)
        saved = self._read_task_stage(request, stage_key, evidence)
        if saved is not None:
            return saved

        def cached(entry):
            return self._read_request_cache(request, entry)

        def invoke(entry, timeout, attempt):
            return self._invoke_stage(request, stage_key, entry, timeout, attempt)

        # Preserve the exact originally requested context and glossary for both
        # free and paid cache lookup before adapting to permitted network models.
        result = self.router_for(name).cached_result(cached, deadline=deadline)
        if result is None:
            data = self._fit(name, data, fixed_semantics=fixed_semantics)
            request = replace(request, data=data)
            new_key = self._task_stage_key(name, data)
            adapted = new_key != stage_key
            if adapted and name != 'glossary':
                stage_key = new_key
                self.context.group_key = stage_key
                saved = self._read_task_stage(request, stage_key, evidence)
                if saved is not None:
                    return saved
            if adapted:
                result = self.router_for(name).cached_result(cached, deadline=deadline)
            if result is None:
                result = self.router_for(name).run(invoke, validate=self._validate_gateway, deadline=deadline)
        self.diagnostics.write_route({'kind': 'route_complete', 'stage': name, 'entry': result.entry.id,
            'model': result.entry.backend.model, 'actual_model': result.value['actual_model'],
            'resolved_model': result.value.get('resolved_model'),
            'gateway_session': self.context.gateway_session, 'request_id': self.context.request_id,
            'duration_ms': round((time.monotonic() - started) * 1000),
            'attempt': result.attempts, 'cached': result.cached,
            'group_key': stage_key, 'run_id': getattr(self.engine, 'run_id', '')})
        record = {**result.value, **evidence, 'backend': result.entry.id, 'request_model': result.entry.backend.model,
                  'stage': name}
        if name == 'glossary':
            # Bind the accepted advisory result to its original source request,
            # keeping its actual trimmed context for inspection and validation.
            # Its cross-task cache still keys the exact fitted wire request.
            record['context'] = data.get('context', [])
        cache_key = request_cache_key(name, data, result.entry.backend,
                                    capabilities=self.profiles[result.entry.id]['capabilities'], scope=self.cache_scope,
                                    stream=self.stream, fidelity=self.fidelity, output_tokens=self.output_tokens)
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_inflight()
            self.store.commit_mt_stage(self.job_id, stage_key, name, record,
                cache_key=None if result.cached else cache_key,
                cache_value=None if result.cached else json.dumps(result.value, ensure_ascii=False),
                counts={'mt_v6_' + name + ('_cache_hit' if result.cached else '_completed'): 1})
        proof = {'key': stage_key, 'actual_model': record['actual_model'], 'request_model': record['request_model'],
                 'resolved_model': _record_model(record, self.identities)}
        if not result.cached:
            proof.update(request_id=self.context.request_id, gateway_session=self.context.gateway_session)
        return record['value'], record['backend'], proof

    def _fragments(self, units):
        fragments = []
        for sentence_index, translation_unit in enumerate(units):
            for cue in translation_unit['cues']:
                part = {'id': str(len(fragments)), 'sentence': str(sentence_index), 'text': cue['text']}
                part['display_ms'] = self.display_durations.get(cue['cue_id'], cue['end_ms'] - cue['begin_ms'])
                fragments.append(part)
        return fragments

    def _fits_group(self, units, *, output_budget=False):
        if units[-1]['cues'][-1]['end_ms'] - units[0]['cues'][0]['begin_ms'] > 60000:
            return False
        sentences = [_sentence(translation_unit, str(i)) for i, translation_unit in enumerate(units)]
        fragments = self._fragments(units)
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
                validate_cue_coverage(source, members)
                def split(start, end):
                    translation_unit = make_translation_unit(source, members, start, end)
                    if self._fits_group([translation_unit], output_budget=True):
                        return [translation_unit]
                    if end - start < 2 or not source.get('words'):
                        # Intact targets use input-only admission, including guarded models.
                        if self._fits_group([translation_unit]):
                            return [translation_unit]
                        raise ToolError('完整句超过模型预算且没有可用的可靠词时间边界。', 'mt_input_budget')
                    middle = (start + end) // 2
                    return split(start, middle) + split(middle, end)
                units = split(0, len(members))
                if len(units) > 1:
                    if pending:
                        result.append(pending)
                        pending = []
                    result.extend([[translation_unit] for translation_unit in units])
                    continue
                translation_unit = units[0]
                if pending and not self._fits_group([*pending, translation_unit], output_budget=True):
                    result.append(pending)
                    pending = []
                pending.append(translation_unit)
            if pending:
                result.append(pending)
        return result

    def glossary_data(self, group, context):
        manual = self.snapshot['quality']['manual_glossary']
        by_source = {}
        for translation_unit in group:
            identity = translation_unit['source']['sentence_id']
            if identity not in by_source:
                by_source[identity] = _sentence(translation_unit, identity)
            else:
                # Distinct adjacent source ranges can contain identical words.
                # Preserve both occurrences and their exact source spacing.
                current = by_source[identity]
                following = _sentence(translation_unit, identity)
                start = min(current.get('char_start', 0), following.get('char_start', 0))
                end = max(current.get('char_end', len(current['text'])),
                          following.get('char_end', len(following['text'])))
                current.update(text=translation_unit['source']['text'][start:end], char_start=start, char_end=end)
        local = [{**translation_unit['source'], 'text': translation_unit['text']} for translation_unit in group]
        return {'sentences': list(by_source.values()), 'manual_glossary': manual_terms(local, manual),
                'context': context, 'glossary': []}

    def glossary(self, group, context):
        data = self.glossary_data(group, context)
        # Advisory failures must remain part of this job's deterministic prefix:
        # otherwise a later retry could silently change an already saved draft's input.
        unavailable_key = _digest({'stage': 'glossary_unavailable', 'request': self._task_stage_key('glossary', data)})
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
        key = self._task_stage_key('semantic_input', original)
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

    def _advisory_stage(self, name, data, **options):
        self.context.advisory_failure = None
        try:
            return self.stage(name, data, fixed_semantics=True, **options)
        except ToolError as error:
            if not isinstance(error, RouteExhausted) and error.code != 'mt_input_budget':
                raise
            self.engine.mt_control.check_start()
            self.context.advisory_failure = error
            return None

    def _review(self, data, ids, originals, forbidden_models):
        """Keep local evidence resumable, without presenting it as a model review."""
        key = self._task_stage_key('local_review', data)
        expected = _local_review_record(data['translations'], [_digest(text.strip()) for text in originals],
                                        source_anchors(originals) if self.fidelity else None)
        saved = None
        if self.local_review:
            try:
                saved = self.store.mt_stage_entry(self.job_id, key)
                if saved is not None:
                    if saved != {'stage': 'local_review', 'record': expected} or not expected['value']['issues']:
                        raise ValueError()
                    if not self.fidelity:
                        return expected['value'], None, {'key': key, 'local': True}
            except (ValueError, KeyError, TypeError):
                raise ToolError('保存的本地复核依据与译文不一致，请用 --reflow 检查来源。', 'cache_mismatch') from None
        result = self._advisory_stage('review', data, ids=ids, forbidden_models=forbidden_models)
        if result is not None or not self.local_review or not expected['value']['issues']:
            return result
        if saved is not None:
            return expected['value'], None, {'key': key, 'local': True}
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_start()
            self.store.commit_mt_stage(self.job_id, key, 'local_review', expected,
                                       counts={'mt_local_review': 1})
        return expected['value'], None, {'key': key, 'local': True}

    def _quality_error(self, members, reason, issues, failure=None):
        unavailable = reason in {'review_unavailable', 'judge_unavailable', 'review_incomplete'}
        group = getattr(self.context, 'group', None)
        location = [f'第 {group + 1} 组'] if type(group) is int else []
        location.append(f'{len(members)} 屏')
        ids = set() if unavailable else {issue['id'] for issue in issues}
        affected = [cue for i, cue in enumerate(members) if str(i) in ids] or members
        if ids:
            location.append(f'疑点 {len(affected)} 屏')
        location.append('时间 ' + timestamp(min(cue['begin_ms'] for cue in affected)) + '–' +
                        timestamp(max(cue['end_ms'] for cue in affected)))
        details = '；'.join(location)
        if unavailable:
            role = '裁决' if reason == 'judge_unavailable' else '复核'
            code = 'mt_judge_unavailable' if reason == 'judge_unavailable' else 'mt_review_unavailable'
            cause = getattr(failure, 'category', '') or getattr(failure, 'code', '')
            detail = f'；原因 {cause}' if cause else ''
            return ToolError(f'字幕逐屏{role}未完成（{details}{detail}），该组草稿和已完成阶段已保存，未发布。'
                             '排除服务、输出上限或请求预算问题后可用 --resume 继续。', code,
                             getattr(failure, 'request_id', ''))
        labels = {'source_uncertain': '原转写存在未确认疑点', 'revision_cycle': '裁决未改变译文或出现循环',
                  'revision_limit': '达到修订上限后仍有疑点'}
        return ToolError(f'字幕逐屏复核尚未通过（{reason}：{labels[reason]}；{details}）。'
                         '该组草稿和已完成阶段已保存，未发布。请核对上述时间段的音视频与原转写；'
                         '单独 --resume 会复用已保存的复核，不能消除该疑点。'
                         '确认依据并调整人工术语或 mt_policy_version 后用 --reflow 重建；'
                         '--reflow 不会重新识别音频。', 'mt_quality_unresolved')

    def translate_group(self, group, glossary, context):
        self.engine.mt_control.check_start()
        members = [cue for translation_unit in group for cue in translation_unit['cues']]
        if not members or all(cue['translated'] for cue in members):
            return
        relevant = [term for term in glossary if any(term_appears(term['source'], translation_unit['text']) for translation_unit in group)]
        local_ids = {translation_unit['source']['sentence_id'] for translation_unit in group}
        relevant.sort(key=lambda term: (term['origin'] != 'manual', term['sentence'] not in local_ids))
        sentences = [_sentence(translation_unit, str(i)) for i, translation_unit in enumerate(group)]
        fragments = self._fragments(group)
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
        reason, issues, failure = '', [], None
        accepted_uncertainty = False
        confirmed_uncertainty = {}
        review_keys, correction_keys, reviewed = [], [], []
        for round_number in range(self.snapshot['quality']['max_revisions'] + 1):
            self.engine.mt_control.check_start()
            # Repeated subtitles have the same review request identity. A
            # no-op correction or a cycle would only replay saved issues.
            if current in reviewed:
                warning = True
                reason = 'revision_cycle'
                break
            review_data = {**data, 'translations': current}
            forbidden_models = {_proof_model(translation_proof)}
            if production is not translation_proof:
                forbidden_models.add(_proof_model(production))
            result = self._review(review_data, ids, originals, forbidden_models)
            if result is None:
                warning = True
                reason, failure = 'review_unavailable', getattr(self.context, 'advisory_failure', None)
                break
            review, _, review_proof = result
            failure = getattr(self.context, 'advisory_failure', None)
            review_keys.append(review_proof['key'])
            reviewed.append(current)
            review = review_with_checks(review, _quality_checks(current, originals,
                                        source_anchors(originals) if self.fidelity else None))
            issues = review['issues']
            if (self.allow_source_uncertain and not review_proof.get('local') and
                    _confirmed_uncertainty(issues, current, confirmed_uncertainty)):
                accepted_uncertainty, warning = True, True
                break
            if self.fidelity and not self.allow_source_uncertain and any(
                    i['category'] == 'source_uncertain' for i in review['issues']):
                warning = True
                reason = 'source_uncertain'
                issues = [issue for issue in issues if issue['category'] == 'source_uncertain']
                break
            if not review['issues']:
                break
            if (round_number == self.snapshot['quality']['max_revisions'] and
                    not (self.allow_source_uncertain and not review_proof.get('local') and
                         _only_source_uncertain(issues))):
                warning = True
                reason = 'revision_limit'
                break
            correction_data = {**review_data, 'issues': review['issues']}
            forbidden_models = {_proof_model(translation_proof)}
            if not review_proof.get('local'):
                forbidden_models.add(_proof_model(review_proof))
            correction = self._advisory_stage('judge', correction_data, ids=ids, originals=originals,
                                              forbidden_models=forbidden_models)
            if correction is None:
                warning = True
                reason, failure = 'judge_unavailable', getattr(self.context, 'advisory_failure', None)
                break
            corrected, origin, production = correction
            if (self.fidelity and round_number < self.snapshot['quality']['max_revisions'] and
                    corrected['translations'] in reviewed and
                    any(issue['category'] != 'source_uncertain' for issue in corrected['issues'])):
                # Preserve the stalled judgment, but give its frozen review one
                # explicitly identified retry. A saved retry is reused on resume.
                correction = self._advisory_stage('judge', {**correction_data, 'repair_reason': 'no_progress'},
                    ids=ids, originals=originals, forbidden_models=forbidden_models)
                if correction is None:
                    warning = True
                    reason, failure = 'judge_unavailable', getattr(self.context, 'advisory_failure', None)
                    correction_keys.append(production['key'])
                    current = corrected['translations']
                    break
                corrected, origin, production = correction
            correction_keys.append(production['key'])
            current = corrected['translations']
            if not review_proof.get('local'):
                _remember_uncertainty(corrected['issues'], current, fragments, confirmed_uncertainty)
            if (self.allow_source_uncertain and not review_proof.get('local') and
                    _only_source_uncertain(corrected['issues'])):
                accepted_uncertainty, warning = True, True
                break
            if not corrected['issues']:
                warning = bool(review_proof.get('local'))
                reason = 'review_incomplete' if warning else ''
                break
            if round_number == self.snapshot['quality']['max_revisions']:
                warning = True
                reason, issues = 'revision_limit', corrected['issues']
                break
        if self.fidelity and warning and not (accepted_uncertainty or self.allow_quality_warning):
            raise self._quality_error(members, reason, issues, failure)
        final_key = _digest({'stage': 'final', 'strategy': 6, 'cues': [c['cue_id'] for c in members],
                             'data': data, 'scope': self.cache_scope, 'profiles': self.snapshot['entries']})
        record = {'backend': origin, 'request_model': production['request_model'],
                  'actual_model': production['actual_model'], 'production_stage_key': production['key'],
                  'resolved_model': _proof_model(production),
                  'translation_stage_key': translation_proof['key'], 'review_stage_keys': review_keys,
                  'correction_stage_keys': correction_keys, 'cue_ids': [cue['cue_id'] for cue in members],
                  'value': {'translations': current}, 'warning': warning, 'stage': 'final'}
        if warning and self.allow_quality_warning and not accepted_uncertainty:
            record.update(warning_reason=reason, warning_ids=sorted({issue['id'] for issue in issues}))
        values = {cue['cue_id']: current[str(i)] for i, cue in enumerate(members)}
        with self.engine.mt_control.lock:
            self.engine.mt_control.check_inflight()
            counts = {'mt_subtitle_completed': len(values), 'mt_quality_warning': int(warning)}
            self.store.commit_mt_stage(self.job_id, final_key, 'final', record, translations=values, origin=origin,
                                       counts=counts)


def groups_for_layout(engine, job_id, manifest, sources, cues, pipeline):
    if 'mt_group_plan' in manifest:
        return restore_group_plan(manifest['mt_group_plan'], sources, cues, manifest['layout_round'])
    groups = pipeline.plan_groups(sources, cues) if pipeline is not None else []
    candidate = copy.deepcopy(manifest)
    candidate['mt_group_plan'] = save_group_plan(groups, sources, cues, manifest['layout_round'])
    engine.commit_local(job_id, manifest, candidate, engine.store.save_mt_group_plan)
    return groups


def _source_uncertainty_warnings(stages, cues):
    """Expose only locations of independently confirmed source uncertainty."""
    history = {stage['key']: stage['record'] for stage in stages}
    active = {cue['cue_id']: cue.get('mt_stage_key') for cue in cues}
    affected = set()
    for stage in stages:
        final = stage['record']
        if stage['stage'] != 'final' or not final.get('warning') or not final['correction_stage_keys']:
            continue
        last = (final['review_stage_keys'][-1] if len(final['review_stage_keys']) > len(final['correction_stage_keys'])
                else final['correction_stage_keys'][-1])
        issues = history[last]['value']['issues']
        fragments = [{'id': str(index), 'sentence': cue_id.rpartition(':')[0]}
                     for index, cue_id in enumerate(final['cue_ids'])]
        confirmed = {}
        for review_key, correction_key in zip(final['review_stage_keys'], final['correction_stage_keys']):
            if history[review_key]['stage'] != 'local_review':
                judgment = history[correction_key]['value']
                _remember_uncertainty(judgment['issues'], judgment['translations'], fragments, confirmed)
        if _confirmed_uncertainty(issues, final['value']['translations'], confirmed):
            affected.update(cue_id for issue in issues
                            if active.get(cue_id := final['cue_ids'][int(issue['id'])]) == stage['key'])
    return [f'原转写疑点（source_uncertain；时间 {timestamp(cue["begin_ms"])}–{timestamp(cue["end_ms"])}）：'
             '独立裁决后仍依据不足，按保存策略保留忠实译文，请听辨音视频。'
            for cue in cues if cue['cue_id'] in affected]


def _quality_warnings(stages, cues):
    active = {}
    for cue in cues:
        active.setdefault(cue.get('mt_stage_key'), []).append(cue)
    labels = {'review_unavailable': '未完成复核', 'judge_unavailable': '疑点裁决未完成',
              'review_incomplete': '仅完成本地检查，未完成复核', 'source_uncertain': '原转写依据不足',
              'revision_cycle': '自动修订未取得进展', 'revision_limit': '自动修订达到上限'}
    warnings = []
    for stage in stages:
        record = stage['record']
        members = active.get(stage['key'], [])
        if stage['stage'] != 'final' or not record.get('warning_reason') or not members:
            continue
        selected = {record['cue_ids'][int(i)] for i in record['warning_ids']}
        affected = [cue for cue in members if cue['cue_id'] in selected] or members
        begin, end = min(c['begin_ms'] for c in affected), max(c['end_ms'] for c in affected)
        warnings.append(f'字幕质量告警（{record["warning_reason"]}；时间 {timestamp(begin)}–{timestamp(end)}）：'
                        f'{labels[record["warning_reason"]]}，按保存策略保留完整译文并发布。')
    return warnings


def process_subtitles(engine, job_id, manifest, settings, scope, stage):
    timing_version = manifest['display_timing_version']
    sources = engine.store.sources(job_id)
    if not sources:
        raise ToolError('任务缺少完整句来源记录。', 'cache_mismatch')
    if manifest.get('layout_complete'):
        if any(cue['decision'] == 'translate' and not cue['translated'] for cue in engine.store.cues(job_id)):
            raise ToolError('已完成排版缺少完整译文。', 'cache_mismatch')
        return
    source_ids = {source['sentence_id']: source for source in sources}
    positions = {source['sentence_id']: index for index, source in enumerate(sources)}
    needs_translation = any(source['decision'] == 'translate' for source in sources)
    pipeline = Pipeline(engine, job_id, manifest, scope) if needs_translation else None
    try:
        if not manifest.get('layout_planned'):
            cues = []
            for source in sources:
                parts = plan_fragments(source, settings)
                if source['decision'] == 'translate':
                    parts = split_cues_for_translation(source, parts, fits=lambda cue: pipeline._fits_group(
                        [{'source': source, 'text': cue['text'], 'cues': [cue]}]))
                cues.extend(parts)
            plan_display_timing(cues, version=timing_version)
            candidate = copy.deepcopy(manifest)
            candidate.update(layout_planned=True, layout_round=0)
            engine.commit_local(job_id, manifest, candidate, engine.store.save_layout, cues)
        while True:
            check_cancel(engine.cancel)
            stage('subtitle_translating')
            cues = engine.store.cues(job_id)
            display_intervals, _ = plan_display_timing(cues, version=timing_version)
            if pipeline is not None:
                pipeline.display_durations = {item['cue_id']: item['end_ms'] - item['begin_ms'] for item in display_intervals}
            groups = groups_for_layout(engine, job_id, manifest, sources, cues, pipeline)
            pending = [index for index, group in enumerate(groups)
                       if any(not cue['translated'] for translation_unit in group for cue in translation_unit['cues'])]
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
                stages = engine.store.mt_stages(job_id)
                if pipeline is not None and pipeline.allow_source_uncertain:
                    warnings.extend(_source_uncertainty_warnings(stages, cues))
                if pipeline is not None and pipeline.allow_quality_warning:
                    warnings.extend(_quality_warnings(stages, cues))
                elif not (pipeline is not None and pipeline.allow_source_uncertain) and any(item['record'].get('warning') for item in stages):
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
            plan_display_timing(revised, version=timing_version)
            engine.commit_local(job_id, manifest, candidate, engine.store.save_layout, revised)
    finally:
        if pipeline is not None:
            pipeline.close()


def _translate_groups(pipeline, groups, sources, positions, *, done, total):
    """Schedule ready translations first; prefix waits never occupy a worker."""
    engine, job_id = pipeline.engine, pipeline.job_id
    progress = pipeline.group_progress
    workers = min(engine.config.settings.mt_workers, max(1, len(groups)))
    prefix = GlossaryPrefix(pipeline, sources)
    glossaries, errors = {}, []
    next_glossary = next_group = 0
    # Include completed but unconsumed windows in the lookahead limit. A slow
    # early window must not let later results accumulate across the whole job.
    windows = {}

    def window_end(start):
        end = min(start + 8, len(groups))
        while end > start + 1:
            units = [translation_unit for group in groups[start:end] for translation_unit in group]
            try:
                pipeline._fit('glossary', pipeline.glossary_data(
                    units, group_context(sources, units, positions)), output_budget=True)
                break
            except ToolError as error:
                if error.code != 'mt_input_budget':
                    raise
                end -= 1
        return end

    def extract(start, end):
        engine.mt_control.check_start()
        pipeline.context.group = start
        progress.set(start, 'glossary')
        units = [translation_unit for group in groups[start:end] for translation_unit in group]
        result = pipeline.glossary(units, group_context(sources, units, positions))
        assigned = {i: [] for i in range(start, end)}
        for term in result:
            for index in range(start, end):
                if any(translation_unit['source']['sentence_id'] == term['sentence'] and
                       term_appears(term['source'], translation_unit['text']) for translation_unit in groups[index]):
                    assigned[index].append(term)
                    break
        progress.set(start, 'waiting')
        return assigned

    def translate(index, glossary):
        engine.mt_control.check_start()
        pipeline.context.group = index
        group = groups[index]
        context = group_context(sources, group, positions)
        pipeline.translate_group(group, glossary, context)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}

        def schedule():
            nonlocal next_glossary, next_group
            while len(futures) < workers and not errors and not engine.mt_control.stopping:
                if next_group in glossaries:
                    index = next_group
                    # Freeze only the prefix about to be consumed, keeping both
                    # terminology and retained memory independent of completion order.
                    glossary = prefix.extend(glossaries.pop(index))
                    next_group += 1
                    for start, end in list(windows.items()):
                        if end <= next_group:
                            del windows[start]
                    members = [cue for translation_unit in groups[index] for cue in translation_unit['cues']]
                    if all(cue['translated'] for cue in members):
                        progress.finish(index)
                        continue
                    futures[executor.submit(translate, index, glossary)] = (index, False)
                elif next_glossary < len(groups) and len(windows) < 2:
                    index = next_glossary
                    end = window_end(index)
                    next_glossary = end
                    windows[index] = end
                    futures[executor.submit(extract, index, end)] = (index, True)
                else:
                    break

        try:
            schedule()
            while futures:
                engine.mt_control.stopping  # Observe external cancellation during silent requests.
                completed, _ = wait(futures, timeout=.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, is_extraction = futures.pop(future)
                    try:
                        value = future.result()
                        if is_extraction:
                            glossaries.update(value)
                        else:
                            done += sum(not cue['translated'] for translation_unit in groups[index] for cue in translation_unit['cues'])
                    except ToolError as error:
                        progress.finish(index)
                        if error.code != 'cancelled':
                            engine.mt_control.stop(error)
                        errors.append(error)
                    if not is_extraction:
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
