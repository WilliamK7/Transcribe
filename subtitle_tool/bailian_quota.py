"""Read-only quota observations scoped to a user-confirmed CLI account."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

MAX_CLI_RESPONSE = 4 * 1024 * 1024
MAX_CLI_INPUT = 64 * 1024
_MAX_SAFE_INTEGER = 2 ** 53 - 1
_CLI_VERSION = '2.7.2'
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_OPENAPI_CONTEXT_FIELDS = frozenset({
    'access_key_id', 'access_key_secret', 'openapi_access_key_id', 'openapi_access_key_secret',
    'security_token', 'console_region', 'console_site', 'console_switch_agent', 'workspace_id',
})


def beijing_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


@dataclass(frozen=True)
class VerifiedIdentity:
    """User confirmation scoped to the exact CLI context fingerprint."""
    workspace: str = field(repr=False)
    key_fingerprint: str = field(repr=False)
    region: str
    context_fingerprint: str = field(repr=False)
    context_version: int = 1


@dataclass(frozen=True)
class ModelObservation:
    kind: str | None
    remaining: int | None
    total: int | None
    expires: date | None
    auto_stop: bool | None
    remaining_percent_known: bool

    def usable(self, today):
        # The CLI exposes a local date only. It may expire at any instant on
        # that date, so stop at the *beginning* of the indicated Beijing day.
        # This checks the reported status, not whether a change has taken effect.
        return (self.kind == 'Text' and self.auto_stop is True and
                self.remaining is not None and self.total is not None and
                0 < self.remaining <= self.total and
                self.remaining_percent_known and self.expires is not None and self.expires > today)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def _integer(value):
    return value if type(value) is int and 0 <= value <= _MAX_SAFE_INTEGER else None


def parse_observation(raw):
    """Return only safe model fields; the result is *not* identity proof.

    Invalid/duplicate models are omitted or marked unknown. Callers must not
    display or persist the raw CLI output, which may change in future versions.
    """
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_CLI_RESPONSE:
            raise ValueError()
        items = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(items, list):
            raise ValueError()
        result, duplicates = {}, set()
        for item in items:
            if not isinstance(item, dict):
                continue
            model = item.get('model')
            if not isinstance(model, str) or not _MODEL.fullmatch(model):
                continue
            if model in result or model in duplicates:
                result.pop(model, None)
                duplicates.add(model)
                continue
            kind = item.get('type')
            if kind not in ('Text', 'Audio', 'Vision', 'Multimodal', 'Embedding', '-'):
                kind = None
            raw_date = item.get('expires')
            try:
                expires = date.fromisoformat(raw_date) if isinstance(raw_date, str) and _DATE.fullmatch(raw_date) else None
            except ValueError:
                expires = None
            stop = item.get('autoStop')
            stop = stop if type(stop) is bool else None
            percent = item.get('remainingPercent')
            percent_known = (not isinstance(percent, bool) and isinstance(percent, (int, float)) and
                             math.isfinite(percent) and 0 <= percent <= 100)
            result[model] = ModelObservation(kind, _integer(item.get('remaining')),
                _integer(item.get('total')), expires, stop, percent_known)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('Invalid quota observation') from None


class QuotaSourceError(Exception):
    """Sanitized CLI failure."""

    def __init__(self):
        super().__init__('Quota query failed')


class CLIQuotaSource:
    """Launch the verified npm CLI directly, without a shell or API Key.

    The current JSON schema is pinned to the inspected version. The npm layout
    is deliberately required on Windows so that terminating the Node process
    does not merely terminate a .cmd wrapper while leaving the query running.
    """
    def __init__(self, *, config_dir=None):
        self._config_dir = config_dir
        self._token = None
        self._token_context = None
        self._token_lock = threading.Lock()

    def _folder(self):
        configured = self._config_dir or os.environ.get('BAILIAN_CONFIG_DIR') or Path.home() / '.bailian'
        return Path(configured).expanduser().resolve()

    def _read_context(self):
        """Read credentials and their identity from one selected profile snapshot."""
        try:
            folder = self._folder()
            path = folder / 'config.json'
            if path.stat().st_size > MAX_CLI_RESPONSE:
                raise ValueError()
            config = json.loads(path.read_bytes(), object_pairs_hook=_unique)
            if not isinstance(config, dict):
                raise ValueError()
            name = config.get('active_config')
            if name in (None, '', 'default'):
                profile, selected = 'default', config
                # Other named profiles cannot affect the selected default.
                selected = {key: value for key, value in selected.items()
                            if key != 'active_config' and not isinstance(value, dict)}
            elif isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
                profile, selected = name, config.get(name)
            else:
                raise ValueError()
            if not isinstance(selected, dict):
                raise ValueError()
            return folder, profile, selected
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise QuotaSourceError() from None

    @staticmethod
    def _openapi_credentials(selected):
        result = {}
        for source, target in (('access_key_id', 'accessKeyId'), ('access_key_secret', 'accessKeySecret'),
                               ('security_token', 'securityToken')):
            value = selected.get(source)
            if source != 'security_token' and (not isinstance(value, str) or not value.strip()):
                value = selected.get('openapi_' + source)
            if isinstance(value, str):
                value = value.strip()
            if source == 'security_token' and value in (None, ''):
                continue
            if (not isinstance(value, str) or not 1 <= len(value) <= 16384 or
                    any(c.isspace() or ord(c) < 32 for c in value)):
                raise QuotaSourceError()
            result[target] = value
        agent = selected.get('console_switch_agent')
        if agent is not None and (type(agent) is not int or not 1 <= agent <= _MAX_SAFE_INTEGER):
            raise QuotaSourceError()
        return result

    def _fingerprint_context(self, snapshot, *, version=1):
        try:
            if type(version) is not int or version not in (1, 2):
                raise ValueError()
            folder, profile, selected = snapshot
            token = selected.get('access_token') or selected.get('accessToken')
            if selected.get('console_site', 'domestic') != 'domestic':
                raise ValueError()
            if version == 2:
                self._openapi_credentials(selected)
                # v2 queries obtain their own token from the confirmed AK/SK;
                # they never use this independent browser/CLI session token.
                selected = {k: v for k, v in selected.items() if k in _OPENAPI_CONTEXT_FIELDS}
            elif not isinstance(token, str) or not token.strip():
                raise ValueError()
            context = {'schema': version, 'cli_version': _CLI_VERSION, 'config_dir': str(folder),
                       'profile': profile, 'selected': selected, 'query_region': 'cn-beijing',
                       'query_site': 'domestic'}
            raw = json.dumps(context, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
            return folder, profile, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise QuotaSourceError() from None

    def _context(self, *, version=1):
        return self._fingerprint_context(self._read_context(), version=version)

    def bound_to(self, proof):
        try:
            return (isinstance(proof, VerifiedIdentity) and
                    bool(_FINGERPRINT.fullmatch(proof.context_fingerprint)) and
                    self._context(version=proof.context_version)[2] == proof.context_fingerprint)
        except (QuotaSourceError, TypeError):
            return False

    def _command(self):
        try:
            cli = shutil.which('bl')
            if not cli or os.name != 'nt' or Path(cli).suffix.lower() != '.cmd':
                raise ValueError()
            folder = Path(cli).parent
            package = folder / 'node_modules' / 'bailian-cli'
            metadata = package / 'package.json'
            if metadata.stat().st_size > 65536:
                raise ValueError()
            info = json.loads(metadata.read_text(encoding='utf-8'))
            script = package / 'dist' / 'bailian.mjs'
            local_node = folder / 'node.exe'
            node = str(local_node) if local_node.is_file() else shutil.which('node')
            if (not isinstance(info, dict) or info.get('name') != 'bailian-cli' or
                    info.get('version') != _CLI_VERSION or not script.is_file() or not node):
                raise ValueError()
            return [node, str(script), 'usage', 'free', '--all', '--sort', 'expires',
                    '--console-region', 'cn-beijing', '--console-site', 'domestic', '--output', 'json']
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise QuotaSourceError() from None

    def _renewal_command(self):
        command = self._command()
        package = Path(command[1]).parent.parent
        # The bridge verifies the dependency versions Node actually resolves.
        return [command[0], str(Path(__file__).with_name('mt_bailian_bridge.mjs')), str(package)]

    def _renewal_input(self, proof):
        snapshot = self._read_context()
        if self._fingerprint_context(snapshot, version=2)[2] != proof.context_fingerprint:
            raise QuotaSourceError()
        with self._token_lock:
            token = self._token if self._token_context == proof else None
        body = json.dumps({'credentials': self._openapi_credentials(snapshot[2]), 'token': token,
                           'switchAgent': snapshot[2].get('console_switch_agent')},
                          ensure_ascii=True).encode('utf-8')
        if len(body) > MAX_CLI_INPUT:
            raise QuotaSourceError()
        return body

    def query(self, *, control, timeout, proof):
        if not self.bound_to(proof):
            raise QuotaSourceError()
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or timeout <= 0):
            raise QuotaSourceError()
        control.check_start()
        allowed = min(float(timeout), 15.0)
        deadline = time.monotonic() + allowed
        # Reserve part of the caller's allowance for terminating and reaping
        # the child; a query timeout must not add a new drain window.
        query_deadline = deadline - min(.5, allowed / 4)
        process, readers = None, []
        output = bytearray()
        too_large, read_failed = threading.Event(), threading.Event()
        # The CLI owns its console login. Do not read or pass unrelated API
        # keys, access tokens, or credentials from the parent environment.
        allowed = ('SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA',
                   'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP')
        environment = {key: value for key in allowed if (value := os.environ.get(key)) is not None}
        # Node's formatDate uses the process-local timezone. Match beijing_today.
        environment['TZ'] = 'Asia/Shanghai'
        environment['DO_NOT_TRACK'] = '1'
        environment['NO_UPDATE_NOTIFIER'] = '1'

        def read_stream(stream, target, limit):
            try:
                while True:
                    block = stream.read(65536)
                    if not block:
                        break
                    if len(target) + len(block) > limit:
                        too_large.set()
                        break
                    target.extend(block)
            except (OSError, ValueError):
                read_failed.set()

        try:
            renewing = proof.context_version == 2
            command = self._renewal_command() if renewing else self._command()
            request = self._renewal_input(proof) if renewing else None
            environment['BAILIAN_CONFIG_DIR'] = str(self._folder())
            if not renewing:
                folder, profile, fingerprint = self._context()
                if fingerprint != proof.context_fingerprint:
                    raise QuotaSourceError()
                environment['BAILIAN_CONFIG_DIR'] = str(folder)
                command = [*command, '--config', profile]
            control.check_start()
            if time.monotonic() >= query_deadline:
                raise QuotaSourceError()
            process = subprocess.Popen(command, stdin=subprocess.PIPE if renewing else subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, close_fds=True, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            readers = [threading.Thread(target=read_stream, args=(process.stdout, output, MAX_CLI_RESPONSE), daemon=True)]
            if renewing:
                def write_input():
                    try:
                        with process.stdin:
                            process.stdin.write(request)
                    except (OSError, ValueError):
                        read_failed.set()
                readers.append(threading.Thread(target=write_input, daemon=True))
            for reader in readers:
                reader.start()
            while True:
                control.check_inflight(drain=False)
                if too_large.is_set() or read_failed.is_set() or time.monotonic() >= query_deadline:
                    raise QuotaSourceError()
                if process.poll() is not None and not any(reader.is_alive() for reader in readers):
                    break
                time.sleep(min(.05, max(0.0, query_deadline - time.monotonic())))
            if process.returncode != 0:
                raise QuotaSourceError()
            if not self.bound_to(proof):
                raise QuotaSourceError()
            if renewing:
                try:
                    envelope = json.loads(output, object_pairs_hook=_unique)
                    if not isinstance(envelope, dict) or set(envelope) != {'token', 'observation'}:
                        raise ValueError()
                    token = envelope['token']
                    if (not isinstance(token, str) or not 1 <= len(token) <= 16384 or
                            any(c.isspace() or ord(c) < 32 for c in token)):
                        raise ValueError()
                    observation = json.dumps(envelope['observation'], ensure_ascii=True).encode('utf-8')
                    parse_observation(observation)
                except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                    raise QuotaSourceError() from None
                with self._token_lock:
                    self._token_context, self._token = proof, token
                return observation
            return bytes(output)
        except (OSError, ValueError, TypeError):
            raise QuotaSourceError() from None
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    pass
                for reader in readers:
                    reader.join(timeout=max(0.0, deadline - time.monotonic()))
                if process.stdout is not None and (not readers or not readers[0].is_alive()):
                    process.stdout.close()
