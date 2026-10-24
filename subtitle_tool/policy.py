from __future__ import annotations

from dataclasses import dataclass, replace

from . import mt
from .common import (ASR_MODEL, MT_MODEL, NORMALIZER_VERSION, PROCESSING_VERSION, SUBTITLE_VERSION,
                     ToolError)
from .media import EXTRACT_RECIPE_VERSION


CONTEXT_VERSION = 1


def current_execution_snapshot(strategy_version=3):
    return dict(format_version=1,
                prompt_version=mt.STRUCTURED_PROMPT_VERSION if strategy_version == 4 else mt.PROMPT_VERSION,
                recovery_version=mt.STRUCTURED_RECOVERY_VERSION if strategy_version == 4 else mt.RECOVERY_VERSION,
                context_version=CONTEXT_VERSION)


def asr_contract(normalizer=NORMALIZER_VERSION):
    return {"asr": ASR_MODEL, "normalizer": normalizer,
            "parameters": {"channel_id": [0], "enable_words": True, "enable_itn": False}}


def _version(value, supported):
    if type(value) is not int or value not in supported:
        raise ToolError("任务执行版本不受支持，请用 --reflow 显式重排。", "cache_version")
    return value


def display_timing_version(manifest):
    return _version(manifest.get('display_timing_version', 0), {0, 1, 2})


@dataclass(frozen=True)
class LayoutPolicy:
    zh_line_width: int
    other_line_width: int
    chinese_first: bool


@dataclass(frozen=True)
class ExecutionPolicy:
    normalizer_version: int
    layout: LayoutPolicy | None
    context_version: int
    display_timing_version: int

    def settings(self, runtime):
        """Saved result settings plus current concurrency and polling parameters."""
        if self.layout is None:
            return runtime
        settings = replace(runtime, zh_line_width=self.layout.zh_line_width,
                           other_line_width=self.layout.other_line_width, chinese_first=self.layout.chinese_first)
        settings.validate()
        return settings


def load_policy(manifest, *, verify_execution=True):
    """Validate saved versions and resolve the settings used by the executor."""
    _version(manifest.get("processing_version", 1), {1, PROCESSING_VERSION})
    subtitle = _version(manifest.get("subtitle_version", 0), {0, 1, SUBTITLE_VERSION})
    display_timing = display_timing_version(manifest)
    layout = None
    if subtitle:
        _version(manifest.get("translation_strategy_version"), {1, 2, 3, 4})
        saved_layout = manifest.get("subtitle_settings", {})
        if not isinstance(saved_layout, dict) or set(saved_layout) != {"zh_line_width", "other_line_width", "chinese_first"}:
            raise ToolError("任务的排版快照不受支持，请用 --reflow 显式重排。", "cache_version")
        layout = LayoutPolicy(**saved_layout)
    saved = manifest.get("contract")
    normalizer = NORMALIZER_VERSION
    if saved:
        if not isinstance(saved, dict):
            raise ToolError("任务的识别模型契约不受支持，请用 --reflow 显式重排。", "cache_version")
        normalizer = _version(saved.get("normalizer"), {1, 2, NORMALIZER_VERSION})
        if saved not in (asr_contract(normalizer), {**asr_contract(normalizer), "mt": MT_MODEL}):
            raise ToolError("任务的识别模型／参数契约不受支持，请检查保存记录。", "cache_version")
    if "normalized_version" in manifest:
        _version(manifest["normalized_version"], {1, 2, NORMALIZER_VERSION})
    plan = manifest.get("media_plan")
    _version(plan.get("extract_recipe_version", 1) if plan else EXTRACT_RECIPE_VERSION,
             {1, EXTRACT_RECIPE_VERSION})
    context_version = 0
    if verify_execution and manifest.get('translation_strategy_version') == 4 and 'execution_snapshot' not in manifest:
        raise ToolError('结构化翻译缺少执行版本，请用 --reflow。', 'cache_version')
    if verify_execution and "execution_snapshot" in manifest:
        snapshot = manifest["execution_snapshot"]
        current = current_execution_snapshot(manifest.get('translation_strategy_version', 0))
        required = set(current) - {"context_version"}
        if (not isinstance(snapshot, dict) or set(snapshot) not in (required, set(current)) or
                any(type(snapshot[k]) is not int or snapshot[k] != current[k] for k in required)):
            raise ToolError("任务的提示词／响应恢复执行版本与当前实现不匹配，请用 --reflow 显式重排。", "cache_version")
        context_version = _version(snapshot.get("context_version", 0), {0, CONTEXT_VERSION})
    return ExecutionPolicy(normalizer, layout, context_version, display_timing)
