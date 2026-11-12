"""Job transitions, recovery and command guidance."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .common import ToolError


VERSION = 1
_STATES = {
    'queued': ('排队', 'ready'),
    'probing': ('检查媒体', 'running'),
    'extracting': ('提取音频', 'running'),
    'uploading': ('上传音频', 'running'),
    'submitting': ('提交识别', 'running'),
    'recognizing': ('识别中', 'running'),
    'downloading_result': ('下载结果', 'running'),
    'segmenting': ('整理字幕', 'running'),
    'subtitle_translating': ('字幕翻译', 'running'),
    'layout_adjusting': ('排版调整', 'running'),
    'writing': ('写入字幕', 'running'),
    'completed': ('完成', 'finished'),
    'skipped': ('已跳过', 'finished'),
    'failed': ('失败', 'blocked'),
    'needs_review': ('需要确认', 'blocked'),
    'submission_unknown': ('提交结果未知', 'blocked'),
    'cancelled': ('已停止', 'stopped'),
    'archived': ('已归档', 'archived'),
}
STATES = {name: label for name, (label, _) in _STATES.items()}
RUNNING = frozenset(name for name, (_, kind) in _STATES.items() if kind == 'running')
# Explicit resume/reflow/retime may reopen an inactive task.
INACTIVE = frozenset({'completed', 'skipped', 'cancelled', 'archived'})
RUNNABLE = RUNNING | {'queued', 'failed', 'needs_review'}


def unknown_submission(manifest, state):
    return not manifest.get('reflow') and (
        state == 'submission_unknown' or
        any(s.get('submitting') and not s.get('task_id') for s in manifest.get('segments', [])))


def recovery_target(state, manifest):
    """Pure startup decision; None preserves the entire saved record."""
    if state in INACTIVE or state == 'submission_unknown' or manifest.get('archived'):
        return None
    if unknown_submission(manifest, state):
        return 'submission_unknown'
    if state in RUNNING:
        return 'queued'
    return None


def _no_unknown_submission(before, after, source, target):
    return not unknown_submission(after, 'queued')


def _run_evidence(before, after, source, target):
    return (target in {'submitting', 'submission_unknown', 'cancelled'} or
            _no_unknown_submission(before, after, source, target))


def _recovery_target(before, after, source, target):
    return target == recovery_target(source, before)


def _resume_target(before, after, source, target):
    expected = 'submission_unknown' if unknown_submission(before, source) else 'queued'
    return target == expected and (target == 'submission_unknown' or _no_unknown_submission(before, after, source, target))


def _submission_resolved(before, after, source, target):
    original, updated = before.get('segments', []), after.get('segments', [])
    pending = [i for i, s in enumerate(original) if s.get('submitting') and not s.get('task_id')]
    if (len(pending) != 1 or [s.get('id') for s in original] != [s.get('id') for s in updated] or
            not _no_unknown_submission(before, after, source, target)):
        return False
    resolved = updated[pending[0]]
    return bool(not resolved.get('submitting') and (resolved.get('task_id') or
                not any(name in resolved for name in ('oss_url', 'upload_expires', 'upload_identity'))))


def _reflow_prepared(before, after, source, target):
    return bool(after.get('reflow') and after.get('normalized'))


def _retime_prepared(before, after, source, target):
    return bool(not unknown_submission(before, source) and after.get('retime') and
                after.get('normalized') and after.get('publication_intent'))


@dataclass(frozen=True)
class Transition:
    sources: frozenset[str]
    targets: frozenset[str]
    guard: Callable | None = None


EVENTS = {
    'run': Transition(RUNNABLE, RUNNING | {'completed', 'skipped', 'failed', 'needs_review',
                                         'submission_unknown', 'cancelled'}, _run_evidence),
    'recover': Transition(RUNNABLE, frozenset({'queued', 'submission_unknown'}), _recovery_target),
    'resume': Transition(RUNNABLE | {'submission_unknown', 'cancelled'}, frozenset({'queued', 'submission_unknown'}),
                         _resume_target),
    'review': Transition(frozenset({'needs_review'}), frozenset({'queued'}), _no_unknown_submission),
    'resolve_submission': Transition(frozenset({'submission_unknown'}), frozenset({'queued'}), _submission_resolved),
    'cancel': Transition(RUNNABLE | {'submission_unknown'}, frozenset({'cancelled', 'submission_unknown'})),
    'reflow': Transition(frozenset(STATES) - {'archived'}, frozenset({'queued'}), _reflow_prepared),
    'retime': Transition(frozenset(STATES) - {'archived', 'submission_unknown'}, frozenset({'queued'}), _retime_prepared),
}


def validate_transition(source, target, event, before, after):
    rule = EVENTS.get(event)
    valid = (rule is not None and source in rule.sources and target in rule.targets and
             not before.get('archived') and
             (rule.guard is None or rule.guard(before, after, source, target)))
    if not valid:
        # Do not interpolate arbitrary state/event values from a damaged record.
        raise ToolError('任务状态转移不合法；原状态和恢复资料已保留，请检查调用的操作。', 'invalid_transition')


def contract():
    """Serializable policy, generated from the exact rules used by Store."""
    return {'version': VERSION,
            'states': {state: {'label': label, 'kind': kind, 'auto_recover': state in RUNNING}
                       for state, (label, kind) in _STATES.items()},
            'events': {name: {'from': sorted(rule.sources), 'to': sorted(rule.targets),
                              'guards': [rule.guard.__name__.removeprefix('_')] if rule.guard else []}
                       for name, rule in EVENTS.items()}}


def describe(job, *, reflow_required=False):
    """Command arguments and inputs derived from saved metadata."""
    state, manifest, job_id = job['state'], job['manifest'], job['id']
    legacy_display_conflict = (job.get('error_code') == 'timeline' and manifest.get('normalized') and
                               manifest.get('display_timing_version') == 2)
    actions = []

    def action(name, label, argv, *, required=None, confirmation=False):
        actions.append({'id': name, 'label': label, 'argv': argv,
                        'required_options': required or {}, 'requires_confirmation': confirmation})

    if state == 'archived' or manifest.get('archived'):
        action('export_archive', '校验并导出归档字幕', ['--export-archive', job_id],
               required={'--output-dir': '输出目录'})
        action('reflow', '从完整归档转写新建任务，可能重新翻译', ['--reflow', job_id],
               required={'--output-dir': '输出目录'})
    elif state in {'completed', 'skipped'}:
        pass
    elif unknown_submission(manifest, state):
        action('bind_task_id', '绑定已知云端任务 ID 后继续', ['--resume', job_id],
               required={'--bind-task-id': '云端任务ID'})
        action('allow_resubmit', '明确接受可能重复计费后重发', ['--resume', job_id, '--allow-resubmit'],
               confirmation=True)
    elif legacy_display_conflict and manifest.get('layout_complete') and not reflow_required:
        action('retime', '复用完整译文并升级显示时间，覆盖已有字幕须明确添加 --overwrite', ['--retime', job_id])
    elif reflow_required or legacy_display_conflict or job.get('error_code') in {'cache_version', 'reflow_required'}:
        action('reflow', '从完整本地转写重建；可能重新翻译，覆盖已有字幕须明确添加 --overwrite', ['--reflow', job_id])
    elif state == 'needs_review':
        action('review', '在交互终端确认音轨、声道或语种后继续', ['--review', job_id])
        if job.get('error_code') in {'audio_track', 'audio_channel'}:
            required = {'--channel': 'left/right/mix'}
            if job['error_code'] == 'audio_track':
                required = {'--track': '音轨索引', **required}
            action('select_input', '非交互恢复可按实际选择明确指定音轨／声道', ['--resume', job_id], required=required)
    elif state in RUNNABLE or state == 'cancelled':
        action('resume', '沿保存的进度恢复任务', ['--resume', job_id])
    label, kind = _STATES.get(state, ('未知状态', 'unknown'))
    return {'version': VERSION, 'label': label, 'kind': kind, 'next_actions': actions}
