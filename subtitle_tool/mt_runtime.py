"""Provider error classification for translation replies."""
from __future__ import annotations

from dataclasses import dataclass


TRANSIENT_TYPES = {'timeout', 'provider_unavailable', 'provider_overloaded', 'server', 'rate_limit_exceeded'}
PERMANENT_TYPES = {
    'context_length_exceeded', 'max_tokens_exceeded', 'token_limit_exceeded', 'string_too_long',
    'authentication', 'permission_denied', 'payment_required', 'invalid_request', 'invalid_prompt',
    'not_found', 'precondition_failed', 'payload_too_large', 'unprocessable',
    'content_policy_violation', 'refusal', 'invalid_image', 'image_too_large', 'image_too_small',
    'unsupported_image_format', 'image_not_found', 'image_download_failed',
}


@dataclass(frozen=True)
class ServiceFailure:
    category: str
    retryable: bool = False


def service_failure(status, body, *, free_router=False):
    errors = []
    top_error = choice_error = finish_error = malformed_type = False
    if isinstance(body, dict):
        if 'error' in body:
            top_error = True
            errors.append(body['error'])
        choices = body.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if (choice.get('finish_reason') == 'content_filter' or
                        isinstance(choice.get('message'), dict) and choice['message'].get('refusal')):
                    return ServiceFailure('content_policy_violation')
                if 'error' in choice:
                    choice_error = True
                    errors.append(choice['error'])
                elif choice.get('finish_reason') == 'error':
                    finish_error = True
    kinds = []
    for error in errors:
        metadata = error.get('metadata') if isinstance(error, dict) else None
        kind = metadata.get('error_type') if isinstance(metadata, dict) else None
        if isinstance(metadata, dict) and 'error_type' in metadata and not isinstance(kind, str):
            malformed_type = True
        kinds.append(kind if isinstance(kind, str) else None)
    if finish_error and not top_error:
        # A top-level error explains its finish markers, but never an explicit choice.error.
        kinds.append(None)
    for kind in kinds:
        if kind in PERMANENT_TYPES and not (free_router and kind == 'not_found'):
            return ServiceFailure(kind)
    if status in {400, 401, 402, 403, 405, 413, 415, 422} or status == 404 and not free_router:
        return ServiceFailure('http_permanent')
    if finish_error and not top_error and not choice_error:
        retryable = (200 <= status < 300 or status in {408, 429} or 500 <= status <= 599 or
                     status == 404 and free_router)
        return ServiceFailure('generation_error', retryable)
    retryable_types = TRANSIENT_TYPES | {'not_found'} if free_router else TRANSIENT_TYPES
    untyped_choice = any(kind is None for kind in kinds) and (
        choice_error or finish_error and not top_error)
    if (malformed_type or untyped_choice or
            any(kind is not None and kind not in retryable_types for kind in kinds)):
        return ServiceFailure('service_unknown')
    if free_router and 'not_found' in kinds:
        retryable = 200 <= status < 300 or status in {404, 408, 429} or 500 <= status <= 599
        return ServiceFailure('not_found', retryable)
    if kinds and all(kind in retryable_types for kind in kinds):
        return ServiceFailure(kinds[0], True)
    if not 200 <= status < 300:
        retryable = status in {408, 429} or 500 <= status <= 599 or status == 404 and free_router
        return ServiceFailure('http_transient' if retryable else 'http_permanent', retryable)
    if kinds:
        return ServiceFailure('service_unknown')
    return None


def bailian_failure(status, body):
    content = service_failure(200, body)
    if content is not None and content.category == 'content_policy_violation':
        return ServiceFailure('content_policy_violation')
    error = body.get('error') if isinstance(body, dict) else None
    code = (error.get('code', error.get('type')) if isinstance(error, dict) else None)
    if code is None and isinstance(body, dict):
        code = body.get('code')
    # This exact status/code pair is the server's free-tier stop signal.
    # Other 403/429 responses are not evidence that a model's quota is empty.
    if status == 403 and code == 'AllocationQuota.FreeTierOnly':
        return ServiceFailure('quota_exhausted')
    permanent = {'InvalidApiKey': 'authentication', 'invalid_api_key': 'authentication',
        'AccessDenied': 'permission_denied', 'access_denied': 'permission_denied',
        'Arrearage': 'payment_required', 'arrearage': 'payment_required',
        'InvalidParameter': 'invalid_request', 'InvalidParameterValue': 'invalid_request',
        'invalid_request_error': 'invalid_request', 'InternalError.Algo.InvalidParameter': 'invalid_request',
        'DataInspectionFailed': 'content_policy_violation', 'data_inspection_failed': 'content_policy_violation',
        'InputDataInspectionFailed': 'content_policy_violation', 'OutputDataInspectionFailed': 'content_policy_violation'}
    transient = {'Throttling.RateQuota', 'Throttling.BurstRate', 'Throttling.AllocationQuota',
        'Throttling.ServiceOverloaded', 'Throttling.ResourceExhausted', 'LimitRequests', 'limit_requests',
        'limit_burst_rate', 'insufficient_quota', 'rate_limit_exceeded', 'InternalError', 'internal_error',
        'InternalError.Algo', 'InternalError.Timeout', 'InternalError.Configuration',
        'InternalError.DataInspection', 'InternalError.TranslationFailed', 'SystemError', 'ModelServiceFailed',
        'RequestTimeOut', 'RequestTimeout', 'ServiceUnavailable'}
    if isinstance(code, str) and code in permanent:
        return ServiceFailure(permanent[code])
    if status in {400, 401, 402, 403, 404, 413, 422}:
        return ServiceFailure('http_permanent')
    if isinstance(code, str) and code in transient:
        rate_codes = {'LimitRequests', 'limit_requests', 'limit_burst_rate', 'insufficient_quota', 'rate_limit_exceeded'}
        category = ('rate_limit_exceeded' if code.startswith('Throttling') or code in rate_codes else
                    'timeout' if code in {'InternalError.Timeout', 'RequestTimeOut', 'RequestTimeout'} else 'server')
        return ServiceFailure(category, True)
    if status in {408, 429} or status >= 500:
        return ServiceFailure('http_transient', True)
    if (not 200 <= status < 300 or error is not None or
            code is not None and (not isinstance(code, (str, int)) or code not in {'Success', '200', 200})):
        return ServiceFailure('service_unknown')
    return None
