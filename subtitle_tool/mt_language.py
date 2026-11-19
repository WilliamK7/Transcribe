"""Contextual language decisions before translation; all text stays in private stages."""
from __future__ import annotations

import copy

from .common import ASR_LANGUAGE_INFERENCE_VERSION, ToolError
from .review import decision_origin
from .subtitles import make_fragment


def _target(source, identity):
    return {'id': identity, 'text': source['text'], 'sentence_id': source['sentence_id'],
            'asr_language': source['language'], 'begin_ms': source['begin_ms'], 'end_ms': source['end_ms']}


def _data(sources, indices):
    targets = set(indices)
    neighbors = set()
    for index in indices:
        for direction in [-1, 1]:
            previous = sources[index]
            for distance in range(1, 5):
                at = index + distance * direction
                if not 0 <= at < len(sources):
                    break
                source = sources[at]
                if (source['sentence_id'].split(':')[0] != previous['sentence_id'].split(':')[0] or
                        max(previous['begin_ms'] - source['end_ms'], source['begin_ms'] - previous['end_ms'], 0) > 60000):
                    break
                if at not in targets:
                    neighbors.add(at)
                previous = source
    context = []
    # Closest evidence survives budget trimming; wire order is restored by the payload builder.
    for at in sorted(neighbors, key=lambda at: (min(abs(at - index) for index in indices), at)):
        source = sources[at]
        item = {key: value for key, value in _target(source, '').items() if key != 'id'}
        item['source_position'] = at
        if decision_origin(source) == 'manual':
            item.update(manual_decision=source['decision'], source_language=source['source_lang'])
        context.append(item)
    return {'sentences': [_target(sources[index], str(number)) for number, index in enumerate(indices)],
            'context': context}


def validate_language_reviews(engine, job_id, manifest, sources):
    from .mt_backend import MTBackend
    from .mt_pipeline import _digest, _record_model, _valid_stage_metadata
    from .mt_pool import saved_route
    from .mt_quality import validate_stage_value
    reviewed = [source for source in sources if 'language_review' in source]
    if not reviewed:
        return
    try:
        route = saved_route(manifest)
        profiles = {entry['id']: entry for entry in route['entries']}
        checked = {}
        for source in reviewed:
            proof = source['language_review']
            if (manifest['contract'].get('language_inference') != ASR_LANGUAGE_INFERENCE_VERSION or
                    decision_origin(source) != 'automatic' or
                    source['source_lang'] != 'auto' or not isinstance(proof, dict) or
                    set(proof) != {'stage_key', 'id'} or not isinstance(proof['stage_key'], str) or
                    not isinstance(proof['id'], str)):
                raise ValueError()
            key, identity = proof['stage_key'], proof['id']
            if key not in checked:
                stored = engine.store.mt_stage_entry(job_id, key)
                record = stored['record']
                if (stored['stage'] != 'language' or record['stage'] != 'language' or
                        record['backend'] not in {'magpie_review', 'magpie_review_fallback'} or
                        not _valid_stage_metadata(record)):
                    raise ValueError()
                backend = MTBackend.from_snapshot(profiles[record['backend']]['backend'])
                if record['request_model'] != backend.model:
                    raise ValueError()
                backend.credential(engine.config)
                _record_model(record, route.get('identities', {}))
                ids = [str(i) for i in range(len(record['source_records']))]
                validate_stage_value('language', record['value'], ids=ids)
                checked[key] = record
            record = checked[key]
            if (identity not in record['value']['decisions'] or
                    record['source_records'][int(identity)] != _digest(_target(source, identity)) or
                    record['value']['decisions'][identity] != source['decision']):
                raise ValueError()
    except (KeyError, TypeError, ValueError, IndexError, ToolError):
        raise ToolError('自动语种决定与保存的模型依据不一致，请用 --reflow 从原始转写重建。', 'cache_mismatch') from None


def review_languages(engine, job_id, manifest, scope):
    """Use the review pool only for unlabeled sources which local rules could not decide."""
    from .mt_pipeline import GroupProgress, Pipeline
    from .mt_routing import RouteExhausted
    if manifest.get('contract', {}).get('language_inference') != ASR_LANGUAGE_INFERENCE_VERSION:
        return
    sources = engine.store.sources(job_id)
    # Resume only unresolved requests; keep every committed model decision.
    indices = [index for index, source in enumerate(sources) if not source['language'] and
               decision_origin(source) == 'automatic' and
               source['decision'] == 'uncertain' and 'language_review' not in source]
    if not indices:
        return
    validate_language_reviews(engine, job_id, manifest, sources)
    pipeline = Pipeline(engine, job_id, manifest, scope)
    progress = GroupProgress(engine, job_id)
    pipeline.group_progress = progress
    progress.start()

    def run_group(indices):
        engine.mt_control.check_start()
        pipeline.context.group = indices[0]
        progress.set(indices[0], 'language')
        try:
            data = pipeline._fit('language', _data(sources, indices), output_budget=True)
        except ToolError as error:
            if error.code != 'mt_input_budget' or len(indices) < 2:
                raise
            middle = len(indices) // 2
            run_group(indices[:middle])
            run_group(indices[middle:])
            return
        value, _, proof = pipeline.stage('language', data, fixed_semantics=True)
        changed = copy.deepcopy(sources)
        for number, index in enumerate(indices):
            decision = value['decisions'][str(number)]
            changed[index].update(decision=decision, source_lang='auto', decision_origin='automatic',
                                  reason=('自动语种复核仍无法确定；请确认是否需要翻译' if decision == 'uncertain' else
                                          '模型结合上下文确认保留原文' if decision == 'original' else
                                          '模型结合上下文确认翻译；MT 自动源语言'),
                                  language_review={'stage_key': proof['key'], 'id': str(number)})
        candidate = copy.deepcopy(manifest)
        validate_language_reviews(engine, job_id, candidate, changed)
        with engine.mt_control.lock:
            engine.mt_control.check_inflight()
            engine.commit_local(job_id, manifest, candidate, engine.store.save_language_review,
                                changed, [make_fragment(source) for source in changed])
        sources[:] = changed
        progress.finish(indices[0])

    try:
        for start in range(0, len(indices), 8):
            run_group(indices[start:start + 8])
    except ToolError as error:
        if not isinstance(error, RouteExhausted) and error.code != 'mt_input_budget':
            raise
        engine.mt_control.check_start()
        warning = '自动语种复核暂未完成；未确定的内容保留为待复核，可用 --resume 重试。'
        if warning not in manifest['warnings']:
            manifest['warnings'].append(warning)
            engine.store.update(job_id, manifest=manifest)
    finally:
        progress.close()
        pipeline.close()
