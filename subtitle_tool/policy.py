from __future__ import annotations

from dataclasses import dataclass, replace

from .common import (ASR_MODEL, ASR_LANGUAGE_INFERENCE_VERSION, LEGACY_ASR_MODEL, DISPLAY_TIMING_VERSION,
                     NORMALIZER_VERSION, PROCESSING_VERSION, SUBTITLE_VERSION, ToolError, asr_contract)
from .media import EXTRACT_RECIPE_VERSION
from .mt_quality import GLOSSARY_SCHEDULE, QUALITY_PROMPT_VERSION
from .mt_fidelity import FIDELITY_VERSION
from .subtitles import ALIGNMENT_VERSION

_LAYOUT_FIELDS = ('zh_line_width', 'other_line_width', 'chinese_first')


def current_execution_snapshot():
    return dict(format_version=1,
                prompt_version=QUALITY_PROMPT_VERSION,
                recovery_version=6,
                context_version=3,
                alignment_version=ALIGNMENT_VERSION,
                transport_version=2,
                content_validation_version=2,
                local_review_version=1,
                fidelity_version=FIDELITY_VERSION)


def subtitle_contract(settings):
    return {'subtitle_version': SUBTITLE_VERSION, 'display_timing_version': DISPLAY_TIMING_VERSION,
            'translation_strategy_version': 6, 'execution_snapshot': current_execution_snapshot(),
            'mt_glossary_schedule': GLOSSARY_SCHEDULE,
            'subtitle_settings': {key: getattr(settings, key) for key in _LAYOUT_FIELDS}}


def saved_asr_contract(manifest):
    """Keep the original recognition identity while allowing a local re-normalization."""
    saved = manifest.get("contract")
    if saved is None:
        # Before the 3.1 upgrade, queued jobs did not yet have an ASR snapshot.
        return asr_contract(LEGACY_ASR_MODEL)
    if not isinstance(saved, dict):
        raise ToolError("任务的识别契约无效，请检查保存记录。", "cache_version")
    expected = asr_contract(saved.get('asr'))
    local_versions = {'normalizer': NORMALIZER_VERSION}
    if saved.get('asr') == ASR_MODEL:
        local_versions['language_inference'] = ASR_LANGUAGE_INFERENCE_VERSION
    if {**saved, **local_versions} != expected:
        raise ToolError("任务的识别模型／参数契约不受支持，请检查保存记录。", "cache_version")
    return expected


def _version(value, expected):
    if type(value) is not int or value != expected:
        raise ToolError("任务执行版本不受支持，请用 --reflow 显式重排。", "cache_version")


@dataclass(frozen=True)
class ExecutionPolicy:
    zh_line_width: int
    other_line_width: int
    chinese_first: bool

    def settings(self, runtime):
        """Saved result settings plus current concurrency and polling parameters."""
        settings = replace(runtime, zh_line_width=self.zh_line_width,
                           other_line_width=self.other_line_width, chinese_first=self.chinese_first)
        settings.validate()
        return settings


def validate_raw_contract(manifest):
    """Validate the media and recognition inputs shared by resume and reflow."""
    if manifest.get('archived'):
        raise ToolError('旧任务已归档；可用 --export-archive 导出字幕，或 --reflow 从转写新建任务。', 'job_archived')
    _version(manifest.get("processing_version"), PROCESSING_VERSION)
    plan = manifest.get("media_plan")
    if plan:
        _version(plan.get("extract_recipe_version") if isinstance(plan, dict) else None, EXTRACT_RECIPE_VERSION)
    saved_asr_contract(manifest)


def load_policy(manifest):
    """Validate the saved execution contract before reusing derived results."""
    validate_raw_contract(manifest)
    _version(manifest.get("subtitle_version"), SUBTITLE_VERSION)
    timing_version = manifest.get('display_timing_version')
    if type(timing_version) is not int or timing_version not in (3, DISPLAY_TIMING_VERSION):
        raise ToolError('任务显示时间版本不受支持，请用 --reflow 显式重排。', 'cache_version')
    _version(manifest.get("translation_strategy_version"), 6)
    saved_layout = manifest.get("subtitle_settings", {})
    if not isinstance(saved_layout, dict) or saved_layout.keys() != set(_LAYOUT_FIELDS):
        raise ToolError("任务的排版快照无效，请用 --reflow 重排。", "cache_version")
    saved = manifest.get("contract")
    if saved:
        _version(saved.get("normalizer"), NORMALIZER_VERSION)
        if saved.get('asr') == ASR_MODEL:
            _version(saved.get('language_inference'), ASR_LANGUAGE_INFERENCE_VERSION)
    if "normalized_version" in manifest:
        _version(manifest["normalized_version"], NORMALIZER_VERSION)
    snapshot = manifest.get('execution_snapshot')
    expected = current_execution_snapshot()
    # A missing transport field is the original whole-response contract. Its
    # saved stage and request keys must remain byte-for-byte reproducible.
    if isinstance(snapshot, dict):
        for field in ('transport_version', 'content_validation_version', 'local_review_version', 'fidelity_version'):
            if field not in snapshot:
                expected.pop(field)
    if (not isinstance(snapshot, dict) or snapshot != expected or
            any(type(value) is not int for value in snapshot.values())):
        raise ToolError('任务执行版本已退役；请用 --reflow 从原始转写重建当前版本任务。', 'cache_version')
    if manifest.get('mt_glossary_schedule') != GLOSSARY_SCHEDULE:
        raise ToolError('任务术语调度版本不受支持，请用 --reflow 从原始转写重建。', 'cache_version')
    return ExecutionPolicy(**saved_layout)
