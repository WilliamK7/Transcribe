"""Read-only Bailian CLI observations and conservative per-model admission.

Quota output does not identify the account. The local binding is explicitly a
user confirmation, not an identity assertion made by the CLI or Alibaba.
The observed switch is required alongside a separate user confirmation that
its change has had time to take effect.
"""
from __future__ import annotations

import json
import hashlib
import itertools
import math
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .common import Cancelled, ToolError, validate_workspace_id
from .mt_routing import RouteFailure


OBSERVATION_TTL = 20.0
FAILURE_RETRY_DELAY = 10.0
MAX_CLI_RESPONSE = 4 * 1024 * 1024
MAX_CLI_ERROR = 64 * 1024
_MAX_SAFE_INTEGER = 2 ** 53 - 1
_CLI_VERSION = '2.7.2'
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}\Z')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_OPENAPI_CONTEXT_FIELDS = frozenset({
    'access_key_id', 'access_key_secret', 'openapi_access_key_id', 'openapi_access_key_secret',
    'security_token', 'console_region', 'console_site', 'console_switch_agent', 'workspace_id',
})
_ACCOUNTS = {}
_ACCOUNTS_LOCK = threading.Lock()
_OBSERVATIONS = itertools.count()


def beijing_today():
    return datetime.now(timezone(timedelta(hours=8))).date()


@dataclass(frozen=True)
class VerifiedIdentity:
    """User-confirmed identity scoped to the exact CLI context fingerprint.

    This does not claim an Alibaba-verified account ID.
    """
    account: str = field(repr=False)
    workspace: str = field(repr=False)
    key_fingerprint: str = field(repr=False)
    region: str
    context_fingerprint: str = field(repr=False)
    context_version: int = 1


@dataclass(frozen=True)
class ModelObservation:
    model: str
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
                self.total > 0 and 0 <= self.remaining <= self.total and
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
            result[model] = ModelObservation(model, kind, _integer(item.get('remaining')),
                _integer(item.get('total')), expires, stop, percent_known)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('Invalid quota observation') from None


def estimate_tokens(payload):
    """Reserve the full UTF-8 request plus output cap and message overhead.

    This deliberately overestimates ordinary text, but is not a billing or
    tokenizer guarantee; server-side auto-stop remains mandatory.
    """
    try:
        if not isinstance(payload, dict) or type(payload.get('max_tokens')) is not int or not 1 <= payload['max_tokens'] <= 32768:
            raise ValueError()
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return len(encoded) + payload['max_tokens'] + 1024
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Invalid token estimate input') from None


class QuotaSourceError(Exception):
    """Sanitized CLI failure; it never contains a command response or stderr."""

    def __init__(self, category='quota_refresh_failed'):
        super().__init__('Quota query failed')
        self.category = ('quota_console_session_invalid' if category == 'quota_console_session_invalid'
                         else 'quota_refresh_failed')


def _cli_failure(returncode, stderr):
    """Recognize one fixed CLI error without exposing its potentially private JSON."""
    if returncode == 3:
        try:
            payload = json.loads(stderr, object_pairs_hook=_unique)
            error = payload.get('error') if isinstance(payload, dict) else None
            if (isinstance(error, dict) and type(error.get('code')) is int and error['code'] == 3 and
                    error.get('message') == 'Console session is not logged in or has expired.'):
                return QuotaSourceError('quota_console_session_invalid')
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            pass
    return QuotaSourceError()


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

    def _fingerprint_context(self, snapshot, *, version=1, require_login=True, require_domestic=True,
                             full_profile=False):
        try:
            if type(version) is not int or version not in (1, 2):
                raise ValueError()
            folder, profile, selected = snapshot
            token = selected.get('access_token') or selected.get('accessToken')
            if require_domestic and selected.get('console_site', 'domestic') != 'domestic':
                raise ValueError()
            if version == 2:
                if require_login:
                    self._openapi_credentials(selected)
                # v2 queries obtain their own token from the confirmed AK/SK;
                # they never use this independent browser/CLI session token.
                selected = {k: v for k, v in selected.items() if
                            (k not in ('access_token', 'accessToken') if full_profile else k in _OPENAPI_CONTEXT_FIELDS)}
            elif require_login and (not isinstance(token, str) or not token.strip()):
                raise ValueError()
            context = {'schema': version, 'cli_version': _CLI_VERSION, 'config_dir': str(folder),
                       'profile': profile, 'selected': selected, 'query_region': 'cn-beijing',
                       'query_site': 'domestic'}
            raw = json.dumps(context, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
            return folder, profile, hashlib.sha256(raw).hexdigest()
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise QuotaSourceError() from None

    def _context(self, *, version=1, require_login=True, require_domestic=True):
        return self._fingerprint_context(self._read_context(), version=version,
            require_login=require_login, require_domestic=require_domestic)

    def binding_context(self):
        snapshot = self._read_context()
        version = 2 if any(k in snapshot[2] for k in (
            'access_key_id', 'access_key_secret', 'openapi_access_key_id', 'openapi_access_key_secret')) else 1
        return version, self._fingerprint_context(snapshot, version=version)

    def binding_fingerprint(self, proof):
        """Normalize an exactly matching early v2 binding without changing its identity."""
        snapshot = self._read_context()
        options = dict(version=proof.context_version, require_login=False, require_domestic=False)
        current = self._fingerprint_context(snapshot, **options)[2]
        if current == proof.context_fingerprint:
            return current
        if proof.context_version == 2 and self._fingerprint_context(
                snapshot, full_profile=True, **options)[2] == proof.context_fingerprint:
            return current
        return None

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
        if len(body) > MAX_CLI_ERROR:
            raise QuotaSourceError()
        return body

    def query(self, *, control, timeout, proof=None):
        if proof is not None and not self.bound_to(proof):
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
        output, error_output = bytearray(), bytearray()
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
            renewing = proof is not None and proof.context_version == 2
            command = self._renewal_command() if renewing else self._command()
            request = self._renewal_input(proof) if renewing else None
            environment['BAILIAN_CONFIG_DIR'] = str(self._folder())
            if proof is not None and not renewing:
                folder, profile, fingerprint = self._context()
                if fingerprint != proof.context_fingerprint:
                    raise QuotaSourceError()
                environment['BAILIAN_CONFIG_DIR'] = str(folder)
                command = [*command, '--config', profile]
            control.check_start()
            if time.monotonic() >= query_deadline:
                raise QuotaSourceError()
            process = subprocess.Popen(command, stdin=subprocess.PIPE if renewing else subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False, close_fds=True, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            readers = [threading.Thread(target=read_stream, args=(stream, target, limit), daemon=True)
                       for stream, target, limit in ((process.stdout, output, MAX_CLI_RESPONSE),
                                                     (process.stderr, error_output, MAX_CLI_ERROR))]
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
                raise _cli_failure(process.returncode, error_output)
            if proof is not None and not self.bound_to(proof):
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
        except (Cancelled, ToolError):
            raise
        except QuotaSourceError:
            raise
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
                for stream, reader in zip((process.stdout, process.stderr), readers):
                    if stream is not None and not reader.is_alive():
                        stream.close()


class _ModelLedger:
    def __init__(self):
        self.remaining = None
        self.pending = 0
        self.generation = 0


class _AccountLedger:
    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.refreshing = False
        self.last_failure_at = None
        self.last_failure_proof = None
        self.observed_at = None
        self.observation_sequence = None
        self.observation_context = None
        self.rows = None
        self.models = {}


def _account_for(proof):
    key = (proof.account, proof.region)
    with _ACCOUNTS_LOCK:
        return _ACCOUNTS.setdefault(key, _AccountLedger())


def _failure(category='quota_unknown'):
    # A missing or exhausted model does not disable this account's other models.
    return RouteFailure(category, scope='entry')


class _Reservation:
    def __init__(self, owner, entry, proof, account, estimate, waited):
        self.owner, self.entry, self.proof, self.account = owner, entry, proof, account
        self.estimate, self.preparation_waited = estimate, waited
        self.claimed = self.sent = self.settled = False
        self.model = None
        self.generation = None

    def _check(self):
        if self.owner._binding(self.entry) != self.proof or not self.owner._source_bound(self.proof):
            raise _failure()
        row = self.account.rows.get(self.entry.backend.model) if self.account.rows is not None else None
        if (not self.owner._fresh(self.account, self.proof) or row is None or
                not self.owner._protected(self.entry, row) or not row.usable(self.owner.today())):
            raise _failure()

    def claim(self):
        if self.claimed:
            raise ValueError('Quota reservation already claimed')
        self.owner.control.check_start()
        with self.account.condition:
            self._check()
            model = self.account.models[self.entry.backend.model]
            if model.remaining is None or model.remaining < self.estimate:
                raise _failure('quota_exhausted')
            model.remaining -= self.estimate
            model.pending += self.estimate
            self.model, self.generation = model, model.generation
            self.claimed = True

    def mark_sent(self):
        if not self.claimed or self.sent:
            raise ValueError('Invalid quota dispatch')
        self.owner.control.check_start()
        with self.account.condition:
            self._check()
            self.sent = True

    def settle(self, usage):
        if not self.claimed or not self.sent or self.settled:
            raise ValueError('Invalid quota settlement')
        if (not isinstance(usage, dict) or set(usage) != {'input_tokens', 'output_tokens'} or
                any(_integer(value) is None for value in usage.values())):
            return
        used = usage['input_tokens'] + usage['output_tokens']
        with self.account.condition:
            adjustment = self.estimate - used
            if adjustment < 0 or self.model.generation == self.generation:
                self.model.remaining = max(0, self.model.remaining + adjustment)
            self.model.pending -= self.estimate
            self.settled = True
            self.account.condition.notify_all()

    def close(self):
        if not self.claimed:
            return
        with self.account.condition:
            if not self.settled:
                if not self.sent and self.model.generation == self.generation:
                    self.model.remaining += self.estimate
                self.model.pending -= self.estimate
            self.claimed = False
            self.account.condition.notify_all()


class BailianQuota:
    def __init__(self, control, *, identity=None, protection=None, source=None, clock=time.monotonic, today=beijing_today,
                 on_event=lambda event: None, observation=None):
        self.control, self.identity, self.protection, self.clock, self.today = control, identity, protection, clock, today
        # A new pipeline/run must refresh mutable cloud state, even when another
        # run's observation has not yet expired. Shared debits are retained.
        self.created_sequence = next(_OBSERVATIONS)
        self.source = source if source is not None else CLIQuotaSource()
        self.on_event = on_event
        self.observation = observation
        # A fixed CLI login error needs user action, not periodic refreshes.
        # Keep it in this run only; a new proof or pipeline may query again.
        self._invalid_sessions = set()
        self._session_lock = threading.Lock()

    def _binding(self, entry):
        if self.identity is None:
            return None
        try:
            proof = self.identity(entry)
            if (not isinstance(proof, VerifiedIdentity) or not isinstance(proof.account, str) or
                    not 1 <= len(proof.account) <= 256 or proof.region != 'cn-beijing' or
                    type(proof.context_version) is not int or proof.context_version not in (1, 2) or
                    not isinstance(proof.key_fingerprint, str) or not _FINGERPRINT.fullmatch(proof.key_fingerprint) or
                    not isinstance(proof.context_fingerprint, str) or not _FINGERPRINT.fullmatch(proof.context_fingerprint) or
                    proof.key_fingerprint != entry.backend.key_fingerprint):
                return None
            workspace = validate_workspace_id(proof.workspace)
            if (proof.workspace != workspace or entry.backend.base_url !=
                    f'https://{workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'):
                return None
            return proof
        except Cancelled:
            raise
        except Exception:
            return None

    def _protected(self, entry, row):
        """Require the local confirmation as well as the observed switch."""
        if self.protection is None:
            return False
        try:
            return self.protection(entry, row) is True
        except Cancelled:
            raise
        except Exception:
            return False

    def _source_bound(self, proof):
        """Check the selected console context before and after a query."""
        try:
            if self._session_invalid(proof):
                return False
            return self.source.bound_to(proof) is True
        except Cancelled:
            raise
        except Exception:
            return False

    def _session_invalid(self, proof):
        with self._session_lock:
            return proof in self._invalid_sessions

    def _fresh(self, account, proof):
        return (account.observed_at is not None and account.observation_sequence is not None and
                account.observation_sequence > self.created_sequence and account.observation_context == proof and
                0 <= self.clock() - account.observed_at < OBSERVATION_TTL)

    def potential(self, entry):
        """Whether basic prerequisites exist, without consulting the network.

        A true result is only a planning hint. Fresh quota and effective
        protection must still be established by reserve/claim/dispatch.
        """
        if entry.tier != 'free' or entry.backend.provider != 'bailian':
            return True
        proof = self._binding(entry)
        return proof is not None and self._protected(entry, None) and self._source_bound(proof)

    def priority(self, entry):
        """Return the date of a known-safe, unexpired observation without I/O."""
        if entry.tier != 'free' or entry.backend.provider != 'bailian':
            return None
        proof = self._binding(entry)
        if proof is None or not self._source_bound(proof):
            return None
        with _ACCOUNTS_LOCK:
            account = _ACCOUNTS.get((proof.account, proof.region))
        if account is None:
            return None
        with account.condition:
            row = account.rows.get(entry.backend.model) if account.rows is not None else None
            model = account.models.get(entry.backend.model)
            if (not self._fresh(account, proof) or
                    row is None or not self._protected(entry, row) or not row.usable(self.today()) or
                    model is None or not model.remaining):
                return None
            return row.expires

    @contextmanager
    def reserve(self, entry, wait_budget, *, estimate):
        if (isinstance(wait_budget, bool) or not isinstance(wait_budget, (int, float)) or
                not math.isfinite(wait_budget) or wait_budget < 0):
            raise ValueError('Invalid quota wait budget')
        if entry.tier != 'free' or entry.backend.provider != 'bailian':
            raise ValueError('Invalid quota route')
        if type(estimate) is not int or not 1 <= estimate <= _MAX_SAFE_INTEGER:
            raise ValueError('Invalid token reservation')
        self.control.check_start()
        measured = [0.0]
        try:
            proof = self._binding(entry)
            if proof is None or not self._protected(entry, None) or not self._source_bound(proof):
                raise _failure()
            account = _account_for(proof)
            self._ensure(entry, proof, account, self.clock() + wait_budget, measured)
        except ToolError as error:
            error.preparation_waited = measured[0]
            raise
        reservation = _Reservation(self, entry, proof, account, estimate, measured[0])
        try:
            yield reservation
        finally:
            reservation.close()

    def _ensure(self, entry, proof, account, deadline, measured):
        while True:
            self.control.check_start()
            with account.condition:
                now = self.clock()
                if self._session_invalid(proof):
                    raise _failure()
                if account.rows is not None and self._fresh(account, proof):
                    row = account.rows.get(entry.backend.model)
                    if row is None or not self._protected(entry, row) or not row.usable(self.today()):
                        raise _failure()
                    if account.models[entry.backend.model].remaining <= 0:
                        raise _failure('quota_exhausted')
                    return
                if account.refreshing:
                    delay = min(.1, max(0.0, deadline - now))
                    if delay <= 0:
                        raise _failure()
                    before = self.clock()
                    try:
                        account.condition.wait(delay)
                    finally:
                        measured[0] += max(0.0, self.clock() - before)
                    continue
                if (deadline <= now or account.last_failure_proof == proof and
                        account.last_failure_at is not None and
                        0 <= now - account.last_failure_at < FAILURE_RETRY_DELAY):
                    raise _failure()
                account.refreshing = True
                break

        rows, cancelled, failure_category = None, False, 'quota_refresh_failed'
        try:
            self.on_event({'kind': 'quota_query'})
            before = self.clock()
            try:
                raw = self.source.query(control=self.control, timeout=min(15.0, max(0.0, deadline - before)), proof=proof)
            finally:
                measured[0] += max(0.0, self.clock() - before)
            self.control.check_start()
            if self.clock() <= deadline and self._binding(entry) == proof and self._source_bound(proof):
                rows = parse_observation(raw)
                if self.observation is not None:
                    self.observation(proof, rows)
                if self._binding(entry) != proof or not self._source_bound(proof):
                    rows = None
        except Cancelled:
            rows = None
            cancelled = True
            raise
        except QuotaSourceError as error:
            rows = None
            failure_category = ('quota_console_session_invalid'
                                if error.category == 'quota_console_session_invalid' else 'quota_refresh_failed')
            if failure_category == 'quota_console_session_invalid':
                with self._session_lock:
                    self._invalid_sessions.add(proof)
        except Exception:
            rows = None
        finally:
            with account.condition:
                account.rows = rows
                account.observed_at = self.clock() if rows is not None else None
                account.observation_sequence = next(_OBSERVATIONS) if rows is not None else None
                account.observation_context = proof if rows is not None else None
                if rows is not None:
                    account.last_failure_at = account.last_failure_proof = None
                elif not cancelled:
                    # Share failures between models, measured from completion.
                    # A new binding context can retry immediately.
                    account.last_failure_at, account.last_failure_proof = self.clock(), proof
                if rows is not None:
                    for model_name, row in rows.items():
                        if not row.usable(self.today()):
                            continue
                        model = account.models.setdefault(model_name, _ModelLedger())
                        available = max(0, row.remaining - model.pending)
                        model.remaining = available if model.remaining is None else min(model.remaining, available)
                        model.generation += 1
                account.refreshing = False
                account.condition.notify_all()
            self.on_event({'kind': 'runtime_quota', 'category': 'quota_observed' if rows is not None else failure_category})
        self.control.check_start()
        if rows is None or self.clock() > deadline:
            raise _failure()
        row = rows.get(entry.backend.model)
        if row is None or not self._protected(entry, row) or not row.usable(self.today()):
            raise _failure()
        if account.models[entry.backend.model].remaining <= 0:
            raise _failure('quota_exhausted')
