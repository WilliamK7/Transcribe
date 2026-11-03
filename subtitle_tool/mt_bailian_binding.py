"""Local, explicitly user-confirmed binding for Bailian free-tier use.

The record contains fingerprints and model names only. It does not establish
account ownership: the user confirms the relationship outside the CLI output.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from .common import Cancelled, ToolError, atomic_json, validate_workspace_id
from .mt_backend import key_fingerprint as fingerprint_key
from .mt_bailian_quota import CLIQuotaSource, VerifiedIdentity, parse_observation


_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_RECORD_LIMIT = 1024 * 1024
_LOCK = threading.RLock()


def _error():
    return ToolError('百炼免费额度绑定或保护确认无效；请检查本机 CLI 登录及模型开关后重新确认。',
                     'mt_quota_binding')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


class LocalBailianBinding:
    """Revalidate a local acknowledgement against the actual active CLI profile.

    ``get_key`` supplies the current ASR key only to compute its fingerprint;
    neither it nor the CLI's raw credentials are written or returned. A missing
    provider always fails closed. Source I/O is local except for the explicit
    read-only query in ``confirm_protection``.
    """
    def __init__(self, data_dir, *, get_key=None, get_scope=None, source=None):
        self.path = Path(data_dir) / 'bailian-quota-binding.json'
        self.get_key = get_key
        self.get_scope = get_scope
        self.source = source if source is not None else CLIQuotaSource()
        self._revoked = set()
        self._invalidated = False

    def _key(self):
        if self.get_key is None:
            raise ValueError()
        key = self.get_key()
        if not isinstance(key, str) or not key or key != key.strip() or any(c.isspace() for c in key):
            raise ValueError()
        return fingerprint_key(key)

    def _scope(self):
        if self.get_scope is None:
            raise ValueError()
        scope = self.get_scope()
        if (not isinstance(scope, (tuple, list)) or len(scope) != 2 or
                any(not isinstance(value, str) for value in scope)):
            raise ValueError()
        return tuple(scope)

    def _invalidate(self, record):
        self._invalidated = True
        record['invalidated'] = True
        atomic_json(self.path, record)
        raise ValueError()

    def _read(self):
        if self.path.stat().st_size > _RECORD_LIMIT:
            raise ValueError()
        record = json.loads(self.path.read_bytes(), object_pairs_hook=_unique)
        required = {'version', 'source', 'region', 'workspace', 'key_fingerprint',
                    'cli_fingerprint', 'confirmed_models', 'invalidated'}
        if (not isinstance(record, dict) or set(record) != required or
                type(record['version']) is not int or record['version'] != 1 or
                record['source'] != 'user_confirmed' or record['region'] != 'cn-beijing' or
                type(record['invalidated']) is not bool or
                not isinstance(record['workspace'], str) or
                record['workspace'] != validate_workspace_id(record['workspace']) or
                not isinstance(record['key_fingerprint'], str) or
                not _FINGERPRINT.fullmatch(record['key_fingerprint']) or
                not isinstance(record['cli_fingerprint'], str) or
                not _FINGERPRINT.fullmatch(record['cli_fingerprint'])):
            raise ValueError()
        models = record['confirmed_models']
        if (not isinstance(models, list) or len(models) > 1000 or
                any(not isinstance(model, str) or not _MODEL.fullmatch(model) for model in models) or
                len(set(models)) != len(models)):
            raise ValueError()
        return record

    def _proof(self, record):
        # The local namespace stays stable across explicit rebindings, so an
        # in-process conservative debit cannot be reset by changing profiles.
        namespace = hashlib.sha256(str(self.path.resolve()).encode('utf-8')).hexdigest()
        return VerifiedIdentity('user-confirmed-local:' + namespace, record['workspace'],
                                record['key_fingerprint'], record['region'],
                                record['cli_fingerprint'])

    def _current(self):
        record = self._read()
        if self._invalidated or record['invalidated']:
            raise ValueError()
        proof = self._proof(record)
        key = self._key()
        # A readable, definitely changed context is different from an I/O or
        # login-status failure: once detected, returning to an old value does
        # not silently restore the user's earlier acknowledgement.
        if key != record['key_fingerprint']:
            self._invalidate(record)
        if self._scope() != (record['workspace'], 'beijing'):
            self._invalidate(record)
        _, _, context = self.source._context(require_login=False, require_domestic=False)
        if context != record['cli_fingerprint']:
            self._invalidate(record)
        if not self.source.bound_to(proof):
            raise ValueError()
        return record, proof

    def current_proof(self):
        """Return a valid current binding for a read-only inspection, if any."""
        try:
            with _LOCK:
                return self._current()[1]
        except Cancelled:
            raise
        except Exception:
            return None

    def confirm_identity(self, *, key_fingerprint, workspace):
        """Record the user's explicit same-main-account confirmation."""
        try:
            workspace = validate_workspace_id(workspace)
            if (not isinstance(key_fingerprint, str) or not _FINGERPRINT.fullmatch(key_fingerprint) or
                    self._key() != key_fingerprint or self._scope() != (workspace, 'beijing')):
                raise ValueError()
            _, _, context = self.source._context()
            record = {'version': 1, 'source': 'user_confirmed', 'region': 'cn-beijing',
                      'workspace': workspace, 'key_fingerprint': key_fingerprint,
                      'cli_fingerprint': context, 'confirmed_models': [], 'invalidated': False}
            with _LOCK:
                if (not self.source.bound_to(self._proof(record)) or self._key() != key_fingerprint or
                        self._scope() != (workspace, 'beijing')):
                    raise ValueError()
                atomic_json(self.path, record)
                self._revoked.clear()
                self._invalidated = False
        except Cancelled:
            raise
        except Exception:
            raise _error() from None

    def identity(self, entry):
        """Return confirmation evidence only while both live contexts match."""
        try:
            with _LOCK:
                _, proof = self._current()
            if (entry.backend.provider != 'bailian' or entry.backend.key_fingerprint != proof.key_fingerprint or
                    entry.backend.base_url != f'https://{proof.workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'):
                return None
            return proof
        except Cancelled:
            raise
        except Exception:
            return None

    def confirm_protection(self, models, *, control, timeout=15.0):
        """After user confirmation, record models currently observed switched on.

        This performs only the CLI's read-only query. A partial result does not
        confirm any of the requested models.
        """
        try:
            if (not isinstance(models, (list, tuple)) or not models or len(models) > 1000 or
                    any(not isinstance(model, str) or not _MODEL.fullmatch(model) for model in models) or
                    len(set(models)) != len(models)):
                raise ValueError()
            with _LOCK:
                _, proof = self._current()
            raw = self.source.query(control=control, timeout=timeout, proof=proof)
            rows = parse_observation(raw)
            self.observe(proof, rows)
            if any(model not in rows or rows[model].auto_stop is not True for model in models):
                raise ValueError()
            with _LOCK:
                record, current = self._current()
                if current != proof:
                    raise ValueError()
                record['confirmed_models'] = sorted(set(record['confirmed_models']).union(models))
                atomic_json(self.path, record)
                self._revoked.difference_update(models)
            return tuple(models)
        except Cancelled:
            raise
        except Exception:
            raise _error() from None

    def observe(self, proof, rows):
        """Persist revocation of every explicitly off model in an observation."""
        try:
            with _LOCK:
                record, current = self._current()
                if proof != current or not isinstance(rows, dict):
                    raise ValueError()
                off = {model for model in record['confirmed_models']
                       if model in rows and rows[model].auto_stop is False}
                if off:
                    self._revoked.update(off)
                    record['confirmed_models'] = [model for model in record['confirmed_models'] if model not in off]
                    atomic_json(self.path, record)
        except Cancelled:
            raise
        except Exception:
            raise _error() from None

    def protection(self, entry, row):
        """Check confirmation; revoke it if a fresh observation says off."""
        try:
            model = entry.backend.model
            with _LOCK:
                record, proof = self._current()
                if (self.identity(entry) != proof or model in self._revoked or
                        model not in record['confirmed_models']):
                    return False
                if row is not None and row.auto_stop is False:
                    # Keep this process safe even if an atomic write fails.
                    self._revoked.add(model)
                    record['confirmed_models'].remove(model)
                    atomic_json(self.path, record)
                    return False
                return row is None or row.auto_stop is True
        except Cancelled:
            raise
        except Exception:
            return False
