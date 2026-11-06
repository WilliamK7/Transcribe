from __future__ import annotations

from dataclasses import dataclass, replace

from .common import (ASR_MODEL, NORMALIZER_VERSION, PROCESSING_VERSION, SUBTITLE_VERSION,
                     ToolError)
from .media import EXTRACT_RECIPE_VERSION
from .mt_quality import QUALITY_PROMPT_VERSION


CONTEXT_VERSION = 1


def current_execution_snapshot(strategy_version=5):
    _version(strategy_version, {5})
    return dict(format_version=1,
                prompt_version=QUALITY_PROMPT_VERSION,
                recovery_version=3,
                context_version=CONTEXT_VERSION)


def asr_contract(normalizer=NORMALIZER_VERSION):
    return {"asr": ASR_MODEL, "normalizer": normalizer,
            "parameters": {"channel_id": [0], "enable_words": True, "enable_itn": False}}


def _version(value, supported):
    if type(value) is not int or value not in supported:
        raise ToolError("任务执行版本不受支持，请用 --reflow 显式重排。", "cache_version")
    return value


def display_timing_version(manifest):
    return _version(manifest.get('display_timing_version'), {2})


@dataclass(frozen=True)
class LayoutPolicy:
    zh_line_width: int
    other_line_width: int
    chinese_first: bool


@dataclass(frozen=True)
class ExecutionPolicy:
    normalizer_version: int
    layout: LayoutPolicy
    context_version: int
    display_timing_version: int
    prompt_version: int

    def settings(self, runtime):
        """Saved result settings plus current concurrency and polling parameters."""
        settings = replace(runtime, zh_line_width=self.layout.zh_line_width,
                           other_line_width=self.layout.other_line_width, chinese_first=self.layout.chinese_first)
        settings.validate()
        return settings


def load_policy(manifest, *, verify_execution=True):
    """Validate saved versions and resolve the settings used by the executor."""
    if manifest.get('archived'):
        raise ToolError('旧任务已归档；可用 --export-archive 导出字幕，或 --reflow 从转写新建任务。', 'job_archived')
    _version(manifest.get("processing_version"), {PROCESSING_VERSION})
    _version(manifest.get("subtitle_version"), {SUBTITLE_VERSION})
    display_timing = display_timing_version(manifest)
    _version(manifest.get("translation_strategy_version"), {5})
    saved_layout = manifest.get("subtitle_settings", {})
    if not isinstance(saved_layout, dict) or set(saved_layout) != {"zh_line_width", "other_line_width", "chinese_first"}:
        raise ToolError("任务的排版快照无效，请用 --reflow 重排。", "cache_version")
    layout = LayoutPolicy(**saved_layout)
    saved = manifest.get("contract")
    normalizer = NORMALIZER_VERSION
    if saved:
        if not isinstance(saved, dict):
            raise ToolError("任务的识别模型契约不受支持，请用 --reflow 显式重排。", "cache_version")
        normalizer = _version(saved.get("normalizer"), {NORMALIZER_VERSION})
        if saved != asr_contract(normalizer):
            raise ToolError("任务的识别模型／参数契约不受支持，请检查保存记录。", "cache_version")
    if "normalized_version" in manifest:
        _version(manifest["normalized_version"], {NORMALIZER_VERSION})
    plan = manifest.get("media_plan")
    _version(plan.get("extract_recipe_version") if plan else EXTRACT_RECIPE_VERSION, {EXTRACT_RECIPE_VERSION})
    snapshot = manifest.get('execution_snapshot')
    current = current_execution_snapshot()
    if verify_execution and (not isinstance(snapshot, dict) or set(snapshot) != set(current) or
            any(type(snapshot[k]) is not int or snapshot[k] != current[k] for k in current)):
        raise ToolError('任务执行版本已退役；请用 --reflow 从原始转写重建当前版本任务。', 'cache_version')
    if verify_execution and manifest.get('mt_glossary_schedule', 'prefix-v1') != 'prefix-v1':
        raise ToolError('任务术语调度版本已退役，请从原始转写重建。', 'cache_version')
    return ExecutionPolicy(normalizer, layout, CONTEXT_VERSION, display_timing, QUALITY_PROMPT_VERSION)
