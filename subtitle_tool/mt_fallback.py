"""Saved fallback policy and per-run group routing, independent of wire prompts."""
from __future__ import annotations

import threading
import re

from .common import MT_MODEL, Cancelled, ToolError
from .mt_backend import MTBackend, OPENROUTER_BASE_URL, bailian_mt_url, saved_mt


def supports_fallback(backend):
    return (backend.provider == 'openai_compatible' and backend.base_url == OPENROUTER_BASE_URL
            and backend.request_version == 2)


def fallback_policy(manifest, choice=None):
    primary = saved_mt(manifest)
    if not supports_fallback(primary):
        if choice == 'bailian':
            raise ToolError('百炼后备仅支持 OpenRouter 请求契约 2。', 'mt_fallback_unsupported')
        return None
    account = manifest.get('account')
    if not isinstance(account, str) or not re.fullmatch(r'[0-9a-f]{64}', account):
        raise ToolError('后备翻译缺少原百炼账户身份。', 'cache_mismatch')
    backend = MTBackend('bailian', bailian_mt_url(manifest), MT_MODEL, account)
    return {'format_version': 1, 'enabled': choice != 'none', 'backend': backend.snapshot()}


def saved_fallback(manifest):
    policy = manifest.get('mt_fallback_policy')
    if policy is None:
        return None
    if (not isinstance(policy, dict) or set(policy) - {'format_version', 'enabled', 'backend', 'migration_snapshot'} or
            type(policy.get('format_version')) is not int or policy['format_version'] != 1 or
            type(policy.get('enabled')) is not bool or not supports_fallback(saved_mt(manifest))):
        raise ToolError('翻译后备策略无效或版本不受支持。', 'cache_mismatch')
    backend = saved_mt({**manifest, 'mt_snapshot': policy.get('backend')})
    if backend.provider != 'bailian' or backend.request_version != 1:
        raise ToolError('后备翻译快照必须对应原百炼账户及工作空间。', 'cache_mismatch')
    return backend


def validate_origins(manifest, records):
    policy = manifest.get('mt_fallback_policy')
    if policy is not None:
        saved_fallback(manifest)
    for record in records:
        origin = record.get('mt_origin')
        if ((origin is not None and (policy is None or not isinstance(origin, str) or
                                    origin not in {'primary', 'fallback'} or not record.get('translated')))
                or policy is not None and record.get('translated') and origin is None):
            raise ToolError('译文来源与保存的翻译后端不一致。', 'cache_mismatch')


class PrimaryControl:
    """Quota exhaustion stops primary admission, while admitted responses can still drain."""
    def __init__(self, control, exhausted):
        self.control, self.exhausted = control, exhausted
        self.cancel, self.created = control.cancel, control.created

    def check_start(self):
        self.control.check_start()
        if self.exhausted.is_set():
            from .mt_runtime import ServiceError
            raise ServiceError('quota_exhausted', code='mt_quota_exhausted')

    def check_inflight(self, **kwargs):
        return self.control.check_inflight(**kwargs)

    def wait(self, seconds):
        until = self.control.clock() + seconds
        while True:
            self.check_start()
            remaining = until - self.control.clock()
            if remaining <= 0:
                return
            self.cancel.wait(min(.1, remaining))


class GroupFallback:
    def __init__(self, control):
        self.control = control
        self.exhausted = threading.Event()
        self.primary_control = PrimaryControl(control, self.exhausted)

    def run(self, primary, fallback, switched):
        self.control.check_start()
        try:
            self.primary_control.check_start()
            return primary()
        except Cancelled:
            raise
        except ToolError as error:
            eligible = (getattr(error, 'retryable', False) or
                        error.code in {'mt_batch_invalid', 'mt_quota_exhausted', 'mt_retry_after'})
            if not eligible:
                self.control.stop(error)
                raise
            self.control.check_start()
            if error.code == 'mt_quota_exhausted':
                self.exhausted.set()
            switched(error)
            self.control.check_start()
            try:
                return fallback()
            except Cancelled:
                raise
            except ToolError as backup:
                self.control.check_start()
                def summary(failure):
                    category = getattr(failure, 'category', 'format_exhausted' if failure.code == 'mt_batch_invalid' else 'local_error')
                    attempts = getattr(failure, 'attempts', 0)
                    count = f'累计 {attempts} 次' if failure.code == 'mt_batch_invalid' else f'{attempts}/4 次'
                    return f'{category}，{count} HTTP 尝试'
                combined = ToolError('OpenRouter：' + summary(error) + '；百炼：' + summary(backup) +
                                     '；两端均未完成当前组，进度已保存。', 'mt_fallback_failed')
                self.control.stop(combined)
                raise combined from None
