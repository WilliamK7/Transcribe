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


def service_failure(status, body):
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
        if kind in PERMANENT_TYPES:
            return ServiceFailure(kind)
    if status in {400, 401, 402, 403, 404, 405, 413, 415, 422}:
        return ServiceFailure('http_permanent')
    if finish_error and not top_error and not choice_error:
        return ServiceFailure('generation_error')
    untyped_choice = any(kind is None for kind in kinds) and (
        choice_error or finish_error and not top_error)
    if (malformed_type or untyped_choice or
            any(kind is not None and kind not in TRANSIENT_TYPES for kind in kinds)):
        return ServiceFailure('service_unknown')
    if kinds and all(kind in TRANSIENT_TYPES for kind in kinds):
        return ServiceFailure(kinds[0])
    if not 200 <= status < 300:
        transient = status in {408, 429} or 500 <= status <= 599
        return ServiceFailure('http_transient' if transient else 'http_permanent')
    if kinds:
        return ServiceFailure('service_unknown')
    return None
