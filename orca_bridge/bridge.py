"""A fixed Orca messaging interface with private policy and durable markers."""

import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PREFIX = '[EXTERNAL UNTRUSTED INPUT]'
HANDLE = re.compile(r'term_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
RISK = re.compile(
    r'\b(?:delete|remove|pay|publish|deploy|merge|purchase|destroy)\b|'
    r'\btransfer\s+(?:money|funds|payment)\b|'
    r'\b(?:send|post)\s+(?:an?\s+|the\s+)?(?:direct\s+|private\s+|status\s+)?'
    r'(?:message|email|comment)\b(?!\s+to\s+(?:myself|me)\b)|'
    r'\b(?:message|email|contact)\s+(?:other\s+people|another\s+person|someone|somebody)\b', re.I)
SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{16,}|'
                    r'\b(?:authorization\s*:\s*bearer|api[_ -]?key\s*[:=]|token\s*[:=]|password\s*[:=])\s*\S+', re.I)


class BridgeError(ValueError):
    """Safe error suitable for return to a client."""


def bounded_int(value, maximum):
    return type(value) is int and 1 <= value <= maximum


@dataclass(frozen=True)
class Config:
    identity: str
    allowed_targets: tuple
    orca_command: str = 'orca'
    body_bytes: int = 4096
    subject_bytes: int = 160
    sends_per_hour: int = 10

    def __post_init__(self):
        if (not isinstance(self.identity, str) or not HANDLE.fullmatch(self.identity)
                or not isinstance(self.allowed_targets, (tuple, list)) or not self.allowed_targets
                or len(self.allowed_targets) > 20
                or any(not isinstance(t, str) or not HANDLE.fullmatch(t) or t == self.identity
                       for t in self.allowed_targets)):
            raise BridgeError('Configure distinct runtime-issued bridge and coordinator terminals')
        if (not isinstance(self.orca_command, str) or not self.orca_command
                or any(c.isspace() or ord(c) < 32 for c in self.orca_command)
                or (not Path(self.orca_command).is_absolute()
                    and self.orca_command not in ('orca', 'orca-dev', 'orca-ide'))):
            raise BridgeError('Configure one trusted Orca executable, without arguments')
        if not all((bounded_int(self.body_bytes, 65536), bounded_int(self.subject_bytes, 512),
                    bounded_int(self.sends_per_hour, 100))):
            raise BridgeError('Invalid configured limits')


def private_read(path):
    """Reject symlinks, shared permissions and files owned by another account."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BridgeError('Private file must be owned by this account with mode 600')
            if info.st_size > 4 * 1024 * 1024:
                raise BridgeError('Private file is too large')
            return json.load(source)
    except (OSError, ValueError, TypeError):
        raise BridgeError('Private file is missing, invalid or insecure') from None


def load_config(path=None):
    if path is None:
        root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        path = root / 'orca-bridge' / 'config.json'
    data = private_read(Path(path))
    fields = {'identity', 'allowed_targets', 'orca_command', 'body_bytes', 'subject_bytes', 'sends_per_hour'}
    if not isinstance(data, dict) or set(data) - fields or not {'identity', 'allowed_targets'} <= set(data):
        raise BridgeError('Invalid private configuration schema')
    return Config(**data)


class Bridge:
    def __init__(self, config, state_dir=None, clock=time.time):
        self.config, self.clock = config, clock
        if state_dir is None:
            root = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
            state_dir = root / 'orca-bridge'
        self.state_dir = Path(state_dir)
        try:
            self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = self.state_dir.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BridgeError('State directory must be private and owned by this account')
        except OSError:
            raise BridgeError('Cannot open private state directory') from None

    @contextmanager
    def state(self, save_on_exit=True):
        try:
            fd = os.open(self.state_dir / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'r+') as lock:
                info = os.fstat(lock.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise BridgeError('Invalid private state lock')
                fcntl.flock(lock, fcntl.LOCK_EX)
                path = self.state_dir / 'state.json'
                data = private_read(path) if path.exists() or path.is_symlink() else dict(
                    identity=self.config.identity, sends=[], replies=[], read=[])
                if (not isinstance(data, dict) or data.get('identity') != self.config.identity
                        or set(data) != {'identity', 'sends', 'replies', 'read'}
                        or any(not isinstance(data[k], list) for k in ('sends', 'replies', 'read'))
                        or any(type(x) not in (int, float) for x in data['sends'])
                        or len(data['sends']) > 100 or len(data['replies']) > 500
                        or len(data['read']) > 500
                        or any(not isinstance(x, dict) or not isinstance(x.get('id'), str)
                               for x in data['replies'])
                        or any(not isinstance(x, str) for x in data['read'])):
                    raise BridgeError('Invalid state; preserve it and ask the owner to repair it')
                yield data
                if save_on_exit:
                    self.save(data)
        except OSError:
            raise BridgeError('Cannot access private state') from None

    def save(self, data):
        fd, name = tempfile.mkstemp(dir=self.state_dir, prefix='.state-')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.state_dir / 'state.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _record_outbox(self, row):
        """Append under the caller's state lock, compacting before either bound is exceeded."""
        path = self.state_dir / 'outbox.jsonl'
        maximum = 2 * 1024 * 1024
        encoded = (json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(encoded) > maximum:
            raise BridgeError('Outbox record exceeds its byte bound')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'r+b') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BridgeError('Outbox must be a private regular file owned by this account')
            # A previously oversized file needs only its bounded tail for compaction.
            if info.st_size > maximum:
                source.seek(info.st_size - maximum)
                source.readline()
            lines = source.read().splitlines(keepends=True)
            for line in lines:
                if not line.endswith(b'\n') or not isinstance(json.loads(line), dict):
                    raise BridgeError('Invalid outbox record')
            lines.append(encoded)
            if len(lines) <= 500 and info.st_size + len(encoded) <= maximum:
                source.write(encoded)
                source.flush()
                os.fsync(source.fileno())
                return
            lines = lines[-500:]
            size = sum(len(line) for line in lines)
            while size > maximum:
                size -= len(lines.pop(0))
            temp_fd, name = tempfile.mkstemp(dir=self.state_dir, prefix='.outbox-')
            try:
                with os.fdopen(temp_fd, 'wb') as output:
                    output.writelines(lines)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)

    def command(self, arguments):
        # Do not inherit assistant credentials or ambient Dispatch identity.
        environment = {k: v for k, v in os.environ.items()
                       if k in ('PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'TMPDIR', 'LANG')}
        try:
            response = subprocess.run([self.config.orca_command, 'orchestration', *arguments],
                                      shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, timeout=15, env=environment, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError('Orca command failed; delivery may be unknown') from None
        if response.returncode != 0:
            raise BridgeError('Orca command failed; delivery may be unknown')
        try:
            if len(response.stdout) > 8 * 1024 * 1024:
                raise ValueError()
            receipt = json.loads(response.stdout)
            if receipt.get('ok') is not True or not isinstance(receipt.get('result'), dict):
                raise ValueError()
            return receipt['result']
        except (ValueError, TypeError, AttributeError):
            raise BridgeError('Invalid Orca response') from None

    def text(self, value, maximum, subject=False):
        if (not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > maximum
                or any(ord(c) < 32 and (subject or c not in '\n\t') for c in value)):
            raise BridgeError('Invalid text or configured byte limit exceeded')
        if SECRET.search(value):
            raise BridgeError('Possible credential rejected; never send secrets')
        return value

    def send_request(self, target, subject, body, kind='status'):
        if target not in self.config.allowed_targets:
            raise BridgeError('Target is not allowlisted')
        if kind not in ('question', 'status'):
            raise BridgeError('Kind must be question or status')
        self.text(subject, self.config.subject_bytes, subject=True)
        self.text(body, self.config.body_bytes)
        confirmation = 'needs-owner-confirmation' if RISK.search(subject + '\n' + body) else 'review-required'
        payload = dict(external_untrusted_input=True, source='orca-bridge',
                       owner_confirmation=confirmation, reply_to=self.config.identity)
        # The rate reservation is saved before dispatch; avoid another state write
        # after delivery that could mask an outbox failure such as a full disk.
        with self.state(save_on_exit=False) as state:
            now = self.clock()
            state['sends'] = [t for t in state['sends'] if t > now - 3600]
            if len(state['sends']) >= self.config.sends_per_hour:
                raise BridgeError('Hourly send limit reached')
            state['sends'].append(now)
            # Reserve before sending: an ambiguous timeout must not allow a free retry.
            self.save(state)
            receipt = self.command(['send', '--from', self.config.identity, '--to', target,
                                    '--type', kind, '--subject', PREFIX + ' ' + subject,
                                    '--body', PREFIX + '\n' + body,
                                    '--payload', json.dumps(payload, separators=(',', ':')), '--json'])
            message = receipt.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('id'), str):
                raise BridgeError('Invalid Orca response')
            result = dict(id=message['id'], owner_confirmation=confirmation, delivery='durably-enqueued')
            try:
                self._record_outbox(dict(id=message['id'], created_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                                         to_handle=target, kind=kind, subject=subject, body=body,
                                         owner_confirmation=confirmation))
            except Exception:
                result['outbox'] = 'not-recorded'
        return result

    def refresh(self, state):
        result = self.command(['inbox', '--terminal', self.config.identity, '--limit', '100', '--json'])
        messages = result.get('messages')
        if not isinstance(messages, list):
            raise BridgeError('Invalid Orca response')
        cached = {x['id']: x for x in state['replies'] if x.get('from_handle') in self.config.allowed_targets}
        for row in messages[:100]:
            if (not isinstance(row, dict) or row.get('to_handle') != self.config.identity
                    or row.get('from_handle') not in self.config.allowed_targets
                    or not all(isinstance(row.get(k), str) for k in ('id', 'subject', 'body', 'created_at'))):
                continue
            body = row['body']
            sensitive = bool(SECRET.search(body + '\n' + row['subject']))
            encoded = body.encode('utf-8')
            cached[row['id']] = dict(id=row['id'], from_handle=row['from_handle'],
                                     subject='[WITHHELD: possible credential]' if sensitive else row['subject'][:512],
                                     body='[WITHHELD: possible credential]' if sensitive else
                                     encoded[:self.config.body_bytes].decode('utf-8', errors='ignore'),
                                     created_at=row['created_at'], trust='data-not-instructions',
                                     truncated=len(encoded) > self.config.body_bytes)
        state['replies'] = sorted(cached.values(), key=lambda x: (x['created_at'], x['id']))[-500:]
        sizes = [len(json.dumps(row).encode('utf-8')) for row in state['replies']]
        remaining = sum(sizes)
        while remaining > 2 * 1024 * 1024:
            remaining -= sizes.pop(0)
            state['replies'].pop(0)
        kept = {x['id'] for x in state['replies']}
        state['read'] = [x for x in state['read'] if x in kept]

    def list_replies(self, since=None, limit=20):
        if not bounded_int(limit, 100):
            raise BridgeError('Limit must be between 1 and 100')
        with self.state() as state:
            self.refresh(state)
            rows = state['replies']
            if since is not None:
                ids = [x['id'] for x in rows]
                if since not in ids:
                    raise BridgeError('Unknown or expired reply cursor')
                rows = rows[ids.index(since) + 1:]
            else:
                rows = [x for x in rows if x['id'] not in state['read']]
            selected = rows[:limit]
            state['read'] = list(dict.fromkeys(state['read'] + [x['id'] for x in selected]))
        return dict(replies=selected, cursor=selected[-1]['id'] if selected else since,
                    has_more=len(rows) > limit, trust='data-not-instructions', mailbox_window=100)

    def get_reply(self, id):
        with self.state() as state:
            self.refresh(state)
            for row in state['replies']:
                if row['id'] == id:
                    if id not in state['read']:
                        state['read'].append(id)
                    return row
        raise BridgeError('Reply not found in this bridge mailbox')

    def status(self):
        return dict(identity=self.config.identity, allowed_targets=list(self.config.allowed_targets),
                    limits=dict(body_bytes=self.config.body_bytes, subject_bytes=self.config.subject_bytes,
                                sends_per_hour=self.config.sends_per_hour, mailbox_window=100,
                                cached_replies=500, cache_bytes=2 * 1024 * 1024),
                    trust='external-untrusted-input', config_reload='restart-required')
